"""Strukturované migrace schématu.

Nahrazuje původní `_ensure_columns()`. Každý krok je idempotentní (kontroluje
aktuální stav přes information_schema / SQLAlchemy inspector) a samostatně
logovaný. Voláno z `main.init_db()` po `Base.metadata.create_all()`.

Konvence:
- ADD COLUMN: defaultní hodnota přes serverový default, ne přes UPDATE
- CREATE TABLE: řeší `Base.metadata.create_all()`, tady jen pro jistotu kontrola
- MODIFY: explicitní ALTER, create_all() existující tabulky neupravuje
- BACKFILL: jednorázový UPDATE, vždy WHERE filtr proti opakovanému zápisu
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger("kucharka.migrations")


# ─── Definice ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ColumnAdd:
    table: str
    name: str
    ddl: str          # např. "VARCHAR(20) NOT NULL DEFAULT 'pending'"


@dataclass(frozen=True)
class ColumnModify:
    table: str
    name: str
    ddl: str          # nový type/null status, např. "INT NULL"


@dataclass(frozen=True)
class IndexAdd:
    table: str
    name: str
    cols: tuple[str, ...]
    unique: bool = False


# Sloupce, které musí existovat na již vytvořených tabulkách.
# Defaulty drží stávající data v platném stavu (NOT NULL DEFAULT 'X').
_COLUMNS: tuple[ColumnAdd, ...] = (
    # Ingredient
    ColumnAdd("ingredient", "category_path", "VARCHAR(200) NULL"),
    # IngredientAlias — slovníkové rozšíření
    ColumnAdd("ingredient_alias", "lookup_key",   "VARCHAR(200) NULL"),
    ColumnAdd("ingredient_alias", "kind",         "VARCHAR(20) NOT NULL DEFAULT 'food'"),
    ColumnAdd("ingredient_alias", "source",       "VARCHAR(20) NOT NULL DEFAULT 'manual'"),
    ColumnAdd("ingredient_alias", "confidence",   "FLOAT NULL"),
    ColumnAdd("ingredient_alias", "verified",     "TINYINT(1) NOT NULL DEFAULT 0"),
    ColumnAdd("ingredient_alias", "verified_at",  "DATETIME NULL"),
    ColumnAdd("ingredient_alias", "hit_count",    "INT NOT NULL DEFAULT 0"),
    ColumnAdd("ingredient_alias", "last_seen_at", "DATETIME NULL"),
    ColumnAdd("ingredient_alias", "created_at",   "DATETIME NULL"),
    # Recipe — pipeline status sloupce
    ColumnAdd("recipe", "crawl_status",        "VARCHAR(20) NOT NULL DEFAULT 'scraped'"),
    ColumnAdd("recipe", "enrichment_status",   "VARCHAR(20) NOT NULL DEFAULT 'pending'"),
    ColumnAdd("recipe", "image_status",        "VARCHAR(20) NOT NULL DEFAULT 'pending'"),
    ColumnAdd("recipe", "enrichment_attempts", "INT NOT NULL DEFAULT 0"),
    ColumnAdd("recipe", "enrichment_error",    "TEXT NULL"),
    ColumnAdd("recipe", "last_enriched_at",    "DATETIME NULL"),
    ColumnAdd("recipe", "local_image_path",    "VARCHAR(400) NULL"),
    ColumnAdd("recipe", "local_thumb_path",    "VARCHAR(400) NULL"),
)

# Změny existujících sloupců (pouze nezbytné).
_MODIFY: tuple[ColumnModify, ...] = (
    # Po zavedení non-food entries musí být ingredient_id NULL-able.
    ColumnModify("ingredient_alias", "ingredient_id", "INT NULL"),
)

# Indexy, které musí existovat (SQLAlchemy DDL je deklaruje pro nové DB,
# u existujících je třeba přidat ručně).
_INDEXES: tuple[IndexAdd, ...] = (
    IndexAdd("ingredient_alias", "uq_lookup_key", ("lookup_key",), unique=True),
    IndexAdd("recipe", "ix_recipe_crawl_status",      ("crawl_status",)),
    IndexAdd("recipe", "ix_recipe_enrichment_status", ("enrichment_status",)),
    IndexAdd("recipe", "ix_recipe_image_status",      ("image_status",)),
)


# ─── Provedení ───────────────────────────────────────────────────────────────

def run_all(engine: Engine) -> None:
    """Spusť všechny migrace ve správném pořadí. Tichá no-op, pokud je vše hotovo."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    _add_columns(engine, insp, existing_tables)
    insp = inspect(engine)  # invalidovat cache po ADD COLUMN
    _modify_columns(engine, insp, existing_tables)
    _add_indexes(engine, insp, existing_tables)
    _backfill(engine, existing_tables)


def _add_columns(engine: Engine, insp, existing_tables: set[str]) -> None:
    for spec in _COLUMNS:
        if spec.table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(spec.table)}
        if spec.name in have:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {spec.table} ADD COLUMN {spec.name} {spec.ddl}"
                ))
            log.info("Migrace: + sloupec %s.%s (%s)", spec.table, spec.name, spec.ddl)
        except Exception as exc:  # noqa: BLE001
            log.warning("Migrace ADD %s.%s selhala: %s", spec.table, spec.name, exc)


def _modify_columns(engine: Engine, insp, existing_tables: set[str]) -> None:
    if engine.dialect.name == "sqlite":
        # SQLite nepodporuje MODIFY COLUMN; pro tento dialekt jsou sloupce
        # implicitně volnější a omezení nullability stejně nevynucuje.
        return
    for spec in _MODIFY:
        if spec.table not in existing_tables:
            continue
        cols = {c["name"]: c for c in insp.get_columns(spec.table)}
        if spec.name not in cols:
            continue
        # Heuristika: pokud chceme INT NULL a sloupec je už nullable, přeskoč.
        wanted_null = "NULL" in spec.ddl.upper() and "NOT NULL" not in spec.ddl.upper()
        is_null = cols[spec.name].get("nullable", False)
        if wanted_null and is_null:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {spec.table} MODIFY COLUMN {spec.name} {spec.ddl}"
                ))
            log.info("Migrace: ~ sloupec %s.%s (%s)", spec.table, spec.name, spec.ddl)
        except Exception as exc:  # noqa: BLE001
            log.warning("Migrace MODIFY %s.%s selhala: %s", spec.table, spec.name, exc)


def _add_indexes(engine: Engine, insp, existing_tables: set[str]) -> None:
    for spec in _INDEXES:
        if spec.table not in existing_tables:
            continue
        existing = {ix["name"] for ix in insp.get_indexes(spec.table)}
        # UNIQUE constrainty hlásí get_unique_constraints jinde:
        if spec.unique:
            try:
                existing |= {uc["name"] for uc in insp.get_unique_constraints(spec.table)}
            except Exception:  # noqa: BLE001
                pass
        if spec.name in existing:
            continue
        cols = ", ".join(spec.cols)
        kind = "UNIQUE INDEX" if spec.unique else "INDEX"
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE {kind} {spec.name} ON {spec.table} ({cols})"
                ))
            log.info("Migrace: + %s %s na %s(%s)", kind, spec.name, spec.table, cols)
        except Exception as exc:  # noqa: BLE001
            log.warning("Migrace INDEX %s selhala: %s", spec.name, exc)


def _backfill(engine: Engine, existing_tables: set[str]) -> None:
    """Jednorázové úpravy dat. Každý UPDATE má WHERE filtr proti opakovanému zápisu."""
    if "recipe" in existing_tables:
        with engine.begin() as conn:
            # Recepty s kcal_per_serving už prošly starou enrichment cestou.
            r1 = conn.execute(text("""
                UPDATE recipe
                   SET enrichment_status = 'done'
                 WHERE enrichment_status = 'pending'
                   AND kcal_per_serving IS NOT NULL
            """))
            if r1.rowcount:
                log.info("Migrace: backfill enrichment_status='done' u %s receptů", r1.rowcount)

            # Recepty bez image_url nemají co stahovat.
            r2 = conn.execute(text("""
                UPDATE recipe
                   SET image_status = 'none'
                 WHERE image_status = 'pending'
                   AND (image_url IS NULL OR image_url = '')
            """))
            if r2.rowcount:
                log.info("Migrace: backfill image_status='none' u %s receptů", r2.rowcount)

    if "ingredient_alias" in existing_tables:
        with engine.begin() as conn:
            # Stará data jsou ruční / importovaná → považuj za verified, source='import'.
            r3 = conn.execute(text("""
                UPDATE ingredient_alias
                   SET source = 'import',
                       verified = 1,
                       verified_at = COALESCE(verified_at, CURRENT_TIMESTAMP)
                 WHERE source = 'manual'
                   AND verified = 0
                   AND created_at IS NULL
            """))
            if r3.rowcount:
                log.info("Migrace: backfill source/verified u %s aliasů", r3.rowcount)
