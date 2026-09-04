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
    fulltext: bool = False  # jen MariaDB/MySQL; na SQLite se přeskočí
    # Stavět na POZADÍ (velké tabulky, ALTER v řádu desítek sekund by
    # blokoval start). Na SQLite (testy, malá data) se staví synchronně.
    background: bool = False


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
    # RecipeTag – classifier.py na tohle spoléhá, ORM mapování scházelo
    # (viz models.py), takže heuristické auto-tagování dosud tiše padalo.
    ColumnAdd("recipe_tag", "source", "VARCHAR(20) NOT NULL DEFAULT 'auto'"),
    ColumnAdd("recipe", "image_status",        "VARCHAR(20) NOT NULL DEFAULT 'pending'"),
    ColumnAdd("recipe", "enrichment_attempts", "INT NOT NULL DEFAULT 0"),
    ColumnAdd("recipe", "enrichment_error",    "TEXT NULL"),
    ColumnAdd("recipe", "last_enriched_at",    "DATETIME NULL"),
    ColumnAdd("recipe", "local_image_path",    "VARCHAR(400) NULL"),
    ColumnAdd("recipe", "local_thumb_path",    "VARCHAR(400) NULL"),
    ColumnAdd("recipe", "kcal_per_100g",       "FLOAT NULL"),
    ColumnAdd("recipe", "total_weight_g",      "FLOAT NULL"),
    # MatchDecision – návrh nové suroviny od LLM (tabulka vznikla dřív bez něj)
    ColumnAdd("match_decision", "suggested_name", "VARCHAR(200) NULL"),
    # MatchDecision – příznak "prošlo kontextovou fází" (default 0 → existující
    # no_match/error položky projdou kontextem při nejbližším běhu)
    ColumnAdd("match_decision", "ctx_tried", "TINYINT(1) NOT NULL DEFAULT 0"),
    # RecipeIngredient – rozhodnutá ne-surovina (záměrně bez ingredient_id);
    # existující řádky označí slovníkový sweep při nejbližším párování
    ColumnAdd("recipe_ingredient", "nonfood", "TINYINT(1) NOT NULL DEFAULT 0"),
    # Recipe – denormalizovaný počet napárovaných surovin pro rychlý výpis
    # (viz models.py); historii plní jednorázová migrace na pozadí
    ColumnAdd("recipe", "ing_total", "INT NULL"),
    # Recipe – normalizovaný text pro hledání (název + postup + suroviny
    # prohnané stemmerem, viz modules/textnorm.py); plní jednorázová
    # migrace na pozadí a pak ingest při každém uložení receptu
    ColumnAdd("recipe", "search_text", "TEXT NULL"),
    # Recipe – klíč pro seskupení variant téhož jídla (viz textnorm.title_key)
    ColumnAdd("recipe", "title_key", "VARCHAR(200) NULL"),
    # Recipe – ručně skrytý recept (nezobrazovat ve výpisech ani v návrzích)
    ColumnAdd("recipe", "hidden", "TINYINT(1) NOT NULL DEFAULT 0"),
    # Recipe – skóre pro úvodní stránku; plní úloha na pozadí (modules/feed.py)
    ColumnAdd("recipe", "feed_score", "FLOAT NULL"),
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
    # Pokrývací index pro dostupnost receptů vůči spíži (GROUP BY recipe_id +
    # filtr ingredient_id IN (...)) – hlavní stránka receptů a "Vařím z" na
    # tenhle dotaz sahají při KAŽDÉM načtení, u 150k+ receptů to bez indexu
    # znatelně brzdilo.
    IndexAdd("recipe_ingredient", "ix_ri_recipe_ingredient", ("recipe_id", "ingredient_id")),
    # Obrácený směr: dostupnost vůči spíži agreguje jen řádky se surovinami
    # ze spíže (WHERE ingredient_id IN …) – bez tohoto indexu je to full scan
    # celé recipe_ingredient při každém načtení hlavní stránky.
    IndexAdd("recipe_ingredient", "ix_ri_ingredient_recipe", ("ingredient_id", "recipe_id"),
             background=True),
    # Fulltext pro hledání receptů (název + postup). ILIKE '%q%' na 100k+
    # řádcích skenuje celou tabulku; MATCH..AGAINST je řádově rychlejší a
    # umí i hledání v postupu.
    #
    # POZOR: první FULLTEXT index na InnoDB tabulce znamená přestavbu celé
    # tabulky (přidává se skrytý FTS_DOC_ID) – u 150k+ receptů klidně mnoho
    # minut. NESMÍ se stavět synchronně při startu: appka by po celou dobu
    # neodpovídala, healthcheck/supervisor by ji zabil, rozdělaný ALTER se
    # odrolloval a při dalším startu začal znovu – nekonečná smyčka „appka
    # nenaběhla". Proto se fulltext indexy staví na POZADÍ (viz run_all) a
    # hledání do té doby automaticky jede přes ILIKE fallback.
    IndexAdd("recipe", "ft_recipe_title_instructions", ("title", "instructions"),
             fulltext=True),
    # Fulltext nad normalizovaným textem (stemmer + bez diakritiky + suroviny).
    # Nad původním textem hledání selhávalo na skloňování: „+péct*" nenajde
    # „pečeme" a „+kuře*" nenajde „kuřecí". Staví se taky na pozadí; dokud
    # není hotový, hledání jede přes starší index nad title+instructions.
    IndexAdd("recipe", "ft_recipe_search_text", ("search_text",), fulltext=True),
    # Tyhle tři jsou čistě výkonové: appka bez nich funguje, jen pomaleji.
    # Na `recipe` (dva FULLTEXT indexy, 171k řádků) je CREATE INDEX přestavba
    # tabulky v řádu minut, takže se staví NA POZADÍ – jinak by každý z nich
    # držel start appky a healthcheck by ji mezitím zabil.
    # Seskupení variant ve výpisu jede přes GROUP BY title_key.
    IndexAdd("recipe", "ix_recipe_title_key", ("title_key",), background=True),
    # Úvodní stránka čte prvních N receptů podle skóre – bez indexu by to
    # znamenalo setřídit celou tabulku při každém načtení.
    IndexAdd("recipe", "ix_recipe_feed_score", ("feed_score",), background=True),
    IndexAdd("recipe", "ix_recipe_hidden", ("hidden",), background=True),
)


# ─── Provedení ───────────────────────────────────────────────────────────────

def run_all(engine: Engine) -> None:
    """Spusť všechny migrace ve správném pořadí. Tichá no-op, pokud je vše hotovo.

    Rychlé migrace běží synchronně; dlouhé (FULLTEXT index = přestavba
    tabulky) se odloží do vlákna na pozadí, ať start appky nic neblokuje.
    """
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    _add_columns(engine, insp, existing_tables)
    insp = inspect(engine)  # invalidovat cache po ADD COLUMN
    _modify_columns(engine, insp, existing_tables)
    _add_indexes(engine, insp, existing_tables, fulltext=False)
    _backfill(engine, existing_tables)

    # Dlouhé indexy na pozadí – appka mezitím normálně jede (hledání má
    # ILIKE fallback, dokud index není hotový).
    pending_ft = _missing_fulltext(engine, insp, existing_tables)
    if pending_ft:
        import threading

        threading.Thread(
            target=_add_fulltext_bg, args=(engine, pending_ft), daemon=True,
            name="migrations-fulltext",
        ).start()

    # Jednorázový přepočet jednotek/gramáže/kcal (viz _reparse_units_bg).
    # Jen MariaDB: na SQLite běží testy a vlákno na pozadí by jim závodilo
    # se zápisy; produkce SQLite nepoužívá.
    if (
        engine.dialect.name != "sqlite"
        and "recipe_ingredient" in existing_tables
        and "app_setting" in existing_tables
    ):
        with engine.begin() as conn:
            marker = conn.execute(text(
                "SELECT value FROM app_setting WHERE `key` = 'mig_reparse_units_v1'"
            )).first()
        if marker is None:
            import threading

            threading.Thread(
                target=_reparse_units_bg, args=(engine,), daemon=True,
                name="migrations-reparse-units",
            ).start()

    # Výkonnostní migrace výpisu receptů: velký index + naplnění ing_total.
    # Jen MariaDB (na SQLite se index staví synchronně výš a ing_total plní
    # backfill/recompute za běhu testů).
    if (
        engine.dialect.name != "sqlite"
        and "recipe_ingredient" in existing_tables
        and "app_setting" in existing_tables
    ):
        pending_idx = [
            spec for spec in _INDEXES
            if spec.background and not spec.fulltext and spec.table in existing_tables
            and spec.name not in {ix["name"] for ix in insp.get_indexes(spec.table)}
        ]
        with engine.begin() as conn:
            ing_total_done = conn.execute(text(
                "SELECT value FROM app_setting WHERE `key` = 'mig_ing_total_v1'"
            )).first() is not None
        if pending_idx or not ing_total_done:
            import threading

            threading.Thread(
                target=_perf_bg, args=(engine, pending_idx, ing_total_done),
                daemon=True, name="migrations-perf",
            ).start()

    # Jednorázové odříznutí názvu receptu ze začátku postupu (viz
    # _strip_title_in_instr_bg). Opět jen MariaDB – na SQLite běží testy.
    if (
        engine.dialect.name != "sqlite"
        and "recipe" in existing_tables
        and "app_setting" in existing_tables
    ):
        with engine.begin() as conn:
            marker = conn.execute(text(
                "SELECT value FROM app_setting WHERE `key` = 'mig_strip_title_instr_v1'"
            )).first()
        if marker is None:
            import threading

            threading.Thread(
                target=_strip_title_in_instr_bg, args=(engine,), daemon=True,
                name="migrations-strip-title",
            ).start()

    # Naplnění recipe.search_text (normalizovaný text pro hledání). Na rozdíl
    # od ostatních migrací platí i pro SQLite – testy hledání ten sloupec
    # potřebují mít naplněný. Tam ale běží SYNCHRONNĚ: databáze je v testu
    # maličká a vlákno na pozadí by závodilo se zápisy testu.
    if "recipe" in existing_tables and "app_setting" in existing_tables:
        with engine.begin() as conn:
            marker = conn.execute(text(
                "SELECT value FROM app_setting WHERE `key` = 'mig_search_text_v2'"
            )).first()
        if marker is None:
            if engine.dialect.name == "sqlite":
                _search_text_bg(engine)
            else:
                import threading

                threading.Thread(
                    target=_search_text_bg, args=(engine,), daemon=True,
                    name="migrations-search-text",
                ).start()

    # UNIQUE na názvu suroviny – přidá se, jakmile jsou duplicity uklizené.
    # Na SQLite se přeskakuje: testy zakládají suroviny volně a index by jim
    # padal na tvrdém omezení, které produkce dostane až po sloučení.
    if (
        engine.dialect.name != "sqlite"
        and "ingredient" in existing_tables
        and "uq_ingredient_name" not in {
            ix["name"] for ix in inspect(engine).get_indexes("ingredient")
        }
    ):
        import threading

        threading.Thread(
            target=_unique_ingredient_name_bg, args=(engine,), daemon=True,
            name="migrations-uq-ingredient",
        ).start()


def _missing_fulltext(engine: Engine, insp, existing_tables: set[str]) -> list[IndexAdd]:
    if engine.dialect.name == "sqlite":
        return []
    out = []
    for spec in _INDEXES:
        if not spec.fulltext or spec.table not in existing_tables:
            continue
        existing = {ix["name"] for ix in insp.get_indexes(spec.table)}
        if spec.name in existing:
            continue
        # Index nad search_text nemá smysl stavět dřív, než je sloupec
        # naplněný – jinak by se hledání přepnulo na prázdný index a recepty
        # by dočasně "zmizely". Postaví se při dalším startu.
        if spec.name == "ft_recipe_search_text" and not _marker_set(
            engine, "mig_search_text_v2"
        ):
            log.info("Migrace: fulltext ft_recipe_search_text počká, "
                     "až se dopočítá recipe.search_text.")
            continue
        out.append(spec)
    return out


def _unique_ingredient_name_bg(engine: Engine) -> None:
    """Zkus přidat UNIQUE na `ingredient.name_cs` – trvalá pojistka proti
    duplicitám, kterou nemůže obejít žádná budoucí zapisovací cesta.

    Nejde přidat rovnou: audit našel v produkci 538 shluků se shodným
    názvem (98× „máslo"), na kterých by ALTER selhal. Proto se nejdřív
    zkontroluje, jestli duplicity ještě jsou, a když ano, index se
    přeskočí a zkusí znovu při dalším startu – typicky po doběhu
    dávkového slučování (`ingredient_merge`).
    """
    try:
        with engine.begin() as conn:
            dupes = conn.execute(text(
                "SELECT COUNT(*) FROM (SELECT LOWER(TRIM(name_cs)) k"
                " FROM ingredient GROUP BY k HAVING COUNT(*) > 1) d"
            )).scalar() or 0
        if dupes:
            log.info(
                "Migrace: UNIQUE na ingredient.name_cs čeká – %s názvů má "
                "zatím víc záznamů (slouč je v administraci).", dupes,
            )
            return
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_ingredient_name ON ingredient (name_cs)"
            ))
        log.info("Migrace: UNIQUE na ingredient.name_cs přidán.")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Migrace UNIQUE na ingredient.name_cs selhala "
            "(zopakuje se při dalším startu): %s", exc,
        )


def _marker_set(engine: Engine, key: str) -> bool:
    """Je jednorázová migrace `key` už hotová? (marker v app_setting)"""
    try:
        with engine.begin() as conn:
            return conn.execute(
                text("SELECT value FROM app_setting WHERE `key` = :k"), {"k": key}
            ).first() is not None
    except Exception:  # noqa: BLE001 – tabulka nemusí ještě existovat
        return False


def _add_fulltext_bg(engine: Engine, specs: list[IndexAdd]) -> None:
    for spec in specs:
        cols = ", ".join(spec.cols)
        log.info(
            "Migrace: stavím FULLTEXT index %s na %s(%s) NA POZADÍ – u velké "
            "tabulky to může trvat minuty; hledání zatím jede přes LIKE.",
            spec.name, spec.table, cols,
        )
        try:
            # kontrola těsně před ALTERem – po restartu může být už hotový
            insp = inspect(engine)
            if spec.name in {ix["name"] for ix in insp.get_indexes(spec.table)}:
                continue
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE FULLTEXT INDEX {spec.name} ON {spec.table} ({cols})"
                ))
            log.info("Migrace: FULLTEXT index %s hotový – hledání přepnuto na fulltext.", spec.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Migrace FULLTEXT %s selhala (hledání zůstává na LIKE): %s",
                        spec.name, exc)


def _perf_bg(engine: Engine, specs: list[IndexAdd], ing_total_done: bool) -> None:
    """Výkonnostní migrace na pozadí, v pořadí: (1) velké indexy (ALTER na
    milionové tabulce = desítky sekund, nesmí blokovat start), (2) jednorázové
    naplnění recipe.ing_total (jeden korelovaný UPDATE, index z kroku 1 mu
    pomáhá). Výpis receptů do té doby ukazuje dostupnost 0/0 – srovná se sám."""
    for spec in specs:
        cols = ", ".join(spec.cols)
        try:
            insp = inspect(engine)
            if spec.name in {ix["name"] for ix in insp.get_indexes(spec.table)}:
                continue
            log.info("Migrace: stavím index %s na %s(%s) NA POZADÍ…",
                     spec.name, spec.table, cols)
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE INDEX {spec.name} ON {spec.table} ({cols})"
                ))
            log.info("Migrace: index %s hotový.", spec.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Migrace INDEX %s (pozadí) selhala: %s", spec.name, exc)

    if not ing_total_done:
        try:
            log.info("Migrace: plním recipe.ing_total NA POZADÍ…")
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE recipe SET ing_total = ("
                    " SELECT COUNT(*) FROM recipe_ingredient"
                    " WHERE recipe_ingredient.recipe_id = recipe.id"
                    "   AND recipe_ingredient.ingredient_id IS NOT NULL)"
                ))
                conn.execute(text(
                    "INSERT INTO app_setting (`key`, value) VALUES ('mig_ing_total_v1', '1')"
                ))
            log.info("Migrace: recipe.ing_total naplněno.")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Migrace ing_total selhala (zopakuje se při dalším startu): %s", exc
            )


def _reparse_units_bg(engine: Engine) -> None:
    """JEDNORÁZOVĚ přeparsuj množství/jednotku všech řádků surovin a přepočítej
    gramáž + kcal. Regex parsery dřív nepoznaly skloňované tvary („3 lžic")
    ani přívlastky („1 čajová lžička") → jednotka None → default „číslo × 60 g"
    → nesmysly typu 3 lžíce oleje = 1591 kcal. Historická data je potřeba
    srovnat podle opravených parserů; nové řádky už jedou správně.

    Běží na pozadí (100k+ řádků), po dávkách; marker se zapisuje až po
    úspěšném doběhu – při restartu uprostřed se prostě pustí znovu
    (idempotentní přepis stejných hodnot)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload, sessionmaker

    from .models import Recipe, RecipeIngredient
    from .modules.enrichment import _parse_amount_unit
    from .modules.nutrition import grams_for, kcal_for, recompute_recipe_kcal

    log.info("Migrace: přepočet jednotek a kcal všech surovin NA POZADÍ…")
    Session = sessionmaker(bind=engine)
    changed_rows = 0
    try:
        db = Session()
        try:
            ids = [r for r in db.scalars(select(Recipe.id)).all()]
        finally:
            db.close()

        CHUNK = 300
        for i in range(0, len(ids), CHUNK):
            db = Session()
            try:
                recipes = db.scalars(
                    select(Recipe)
                    .where(Recipe.id.in_(ids[i : i + CHUNK]))
                    .options(
                        selectinload(Recipe.ingredients)
                        .selectinload(RecipeIngredient.ingredient)
                    )
                ).all()
                for r in recipes:
                    touched = False
                    for ri in r.ingredients:
                        if not ri.raw_text:
                            continue
                        amount, unit = _parse_amount_unit(ri.raw_text)
                        ing = ri.ingredient if ri.ingredient_id else None
                        grams = grams_for(amount, unit, ing)
                        kcal = kcal_for(grams, ing)
                        if (
                            amount != ri.amount or unit != ri.unit
                            or grams != ri.grams or kcal != ri.kcal
                        ):
                            ri.amount, ri.unit, ri.grams, ri.kcal = amount, unit, grams, kcal
                            touched = True
                            changed_rows += 1
                    if touched:
                        recompute_recipe_kcal(r)
                db.commit()
            finally:
                db.close()

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO app_setting (`key`, value) VALUES ('mig_reparse_units_v1', '1')"
            ))
        log.info(
            "Migrace: přepočet jednotek hotový – upraveno %s řádků surovin.",
            changed_rows,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Migrace přepočtu jednotek selhala (zopakuje se při dalším startu): %s",
            exc,
        )


def _strip_title_in_instr_bg(engine: Engine) -> None:
    """JEDNORÁZOVĚ odřízni z postupů úvodní opakování názvu receptu.

    Některé zdroje (bestrecepty.cz nejvíc) dávají do schema.org
    recipeInstructions jako první řádek název receptu. Nový scraper to už
    ořezává při ingestu, historická data je potřeba srovnat.

    Běží na pozadí po dávkách; marker se zapisuje až po úspěšném doběhu –
    funkce je idempotentní, takže restart uprostřed nevadí (podruhé už
    ořezaný postup názvem nezačíná a nezmění se)."""
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from .models import Recipe
    from .modules.scraper import strip_title_prefix

    log.info("Migrace: ořez názvu ze začátku postupu NA POZADÍ…")
    Session = sessionmaker(bind=engine)
    changed = 0
    try:
        db = Session()
        try:
            ids = list(db.scalars(select(Recipe.id)).all())
        finally:
            db.close()

        CHUNK = 500
        for i in range(0, len(ids), CHUNK):
            db = Session()
            try:
                for r in db.scalars(
                    select(Recipe).where(Recipe.id.in_(ids[i : i + CHUNK]))
                ).all():
                    fixed = strip_title_prefix(r.instructions, r.title)
                    if fixed != r.instructions:
                        r.instructions = fixed
                        changed += 1
                db.commit()
            finally:
                db.close()

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO app_setting (`key`, value)"
                " VALUES ('mig_strip_title_instr_v1', '1')"
            ))
        log.info("Migrace: ořez názvu hotový – upraveno %s postupů.", changed)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Migrace ořezu názvu selhala (zopakuje se při dalším startu): %s", exc
        )


def _search_text_bg(engine: Engine) -> None:
    """JEDNORÁZOVĚ naplň recipe.search_text a recipe.title_key.

    Fulltext index nad původním textem selhával na skloňování – „+péct*"
    nenajde „pečeme". Sloupec drží název, postup A suroviny prohnané
    stemmerem (viz modules/textnorm.py); nové recepty ho dostávají při
    ingestu, historii doplní tahle migrace.

    `title_key` seskupuje varianty téhož jídla ve výpisu (viz
    textnorm.title_key); plní se v témže průchodu, ať se tabulka nečte dvakrát.

    Idempotentní: bere jen řádky, kterým některý sloupec chybí, takže restart
    uprostřed nevadí. Marker se zapisuje až po úplném doběhu."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload, sessionmaker

    from .models import Recipe
    from .modules.textnorm import refresh_search_text

    log.info("Migrace: plním recipe.search_text a recipe.title_key…")
    Session = sessionmaker(bind=engine)
    filled = 0
    try:
        db = Session()
        try:
            ids = list(db.scalars(
                select(Recipe.id).where(
                    Recipe.search_text.is_(None) | Recipe.title_key.is_(None)
                )
            ).all())
        finally:
            db.close()

        CHUNK = 500
        for i in range(0, len(ids), CHUNK):
            db = Session()
            try:
                for r in db.scalars(
                    select(Recipe)
                    .where(Recipe.id.in_(ids[i : i + CHUNK]))
                    .options(selectinload(Recipe.ingredients))
                ).all():
                    refresh_search_text(r)
                    filled += 1
                db.commit()
            finally:
                db.close()

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO app_setting (`key`, value)"
                " VALUES ('mig_search_text_v2', '1')"
            ))
        log.info("Migrace: search_text a title_key naplněny u %s receptů.", filled)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Migrace search_text/title_key selhala (zopakuje se při dalším startu): %s", exc
        )


def _add_columns(engine: Engine, insp, existing_tables: set[str]) -> None:
    """Doplň chybějící sloupce – všechny pro jednu tabulku JEDNÍM příkazem.

    Proč jedním: `recipe` má dva FULLTEXT indexy a InnoDB na takové tabulce
    neumí ADD COLUMN „instantně" – přestavuje celou tabulku a znovu tokenizuje
    oba fulltexty. U 171 tisíc receptů je to minuty. Když se sloupce přidávaly
    po jednom, byla to jedna přestavba NA SLOUPEC: čtyři nové sloupce = čtyři
    přestavby a start appky stál přes deset minut (viz "Waiting for application
    startup" v logu). Sloučené do jednoho ALTERu je přestavba jen jedna.

    Nejdřív se zkusí ALGORITHM=INSTANT: na tabulkách bez fulltextu je pak
    přidání sloupce okamžité. Když ho engine pro danou změnu neumí, spadne to
    na obyčejný ALTER.
    """
    by_table: dict[str, list[ColumnAdd]] = {}
    for spec in _COLUMNS:
        if spec.table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(spec.table)}
        if spec.name in have:
            continue
        by_table.setdefault(spec.table, []).append(spec)

    # SQLite umí v jednom ALTERu jen jeden ADD COLUMN – tam zůstává po jednom.
    single = engine.dialect.name == "sqlite"
    for table, specs in by_table.items():
        groups = [[s] for s in specs] if single else [specs]
        for group in groups:
            adds = ", ".join(f"ADD COLUMN {s.name} {s.ddl}" for s in group)
            names = ", ".join(s.name for s in group)
            log.info("Migrace: přidávám do %s sloupce %s – u velké tabulky "
                     "s fulltextem to znamená přestavbu a může to trvat minuty.",
                     table, names)
            ok = False
            if not single:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f"ALTER TABLE {table} {adds}, ALGORITHM=INSTANT"
                        ))
                    ok = True
                    log.info("Migrace: + sloupce %s.%s (okamžitě)", table, names)
                except Exception:  # noqa: BLE001 – engine to pro tuhle změnu neumí
                    pass
            if ok:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} {adds}"))
                log.info("Migrace: + sloupce %s.%s", table, names)
            except Exception as exc:  # noqa: BLE001
                log.warning("Migrace ADD %s.%s selhala: %s", table, names, exc)


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


def _add_indexes(engine: Engine, insp, existing_tables: set[str], *, fulltext: bool = True) -> None:
    for spec in _INDEXES:
        if spec.table not in existing_tables:
            continue
        if spec.fulltext and (not fulltext or engine.dialect.name == "sqlite"):
            continue  # fulltext staví _add_fulltext_bg; SQLite syntaxi nezná
        if spec.background and engine.dialect.name != "sqlite":
            continue  # velké indexy staví _perf_bg; na SQLite je stavba levná
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
        kind = "UNIQUE INDEX" if spec.unique else "FULLTEXT INDEX" if spec.fulltext else "INDEX"
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

    if "match_decision" in existing_tables and "app_setting" in existing_tables:
        with engine.begin() as conn:
            # JEDNORÁZOVÉ znovuotevření rozhodnutí "no_match" ze staré verze
            # pipeline (bez suggested_name = model ještě neuměl navrhnout
            # založení nové suroviny). Blokovala nová dopárování: "bazalka",
            # "ztužovač šlehačky" apod. zůstaly navždy bez shody, přestože
            # dnešní běh by je vyřešil. Smazání = příští běh se zeptá znovu.
            # Ruční rozhodnutí (ignored/nonfood/applied) se nedotýká.
            # Marker v app_setting, protože i NOVÁ pipeline legitimně ukládá
            # no_match bez návrhu – bez markeru by se mazaly při každém startu.
            marker = conn.execute(text(
                "SELECT value FROM app_setting WHERE `key` = 'mig_reopen_no_match_v1'"
            )).first()
            if marker is None:
                r4 = conn.execute(text("""
                    DELETE FROM match_decision
                     WHERE status = 'no_match'
                       AND suggested_name IS NULL
                """))
                r5 = conn.execute(text("""
                    UPDATE match_decision SET attempts = 0
                     WHERE status = 'error' AND attempts > 0
                """))
                conn.execute(text(
                    "INSERT INTO app_setting (`key`, value) VALUES ('mig_reopen_no_match_v1', '1')"
                ))
                if r4.rowcount or r5.rowcount:
                    log.info(
                        "Migrace: znovuotevřeno %s starých 'no_match' rozhodnutí a "
                        "resetováno %s chybových (nová pipeline umí navrhnout/založit surovinu)",
                        r4.rowcount, r5.rowcount,
                    )

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
