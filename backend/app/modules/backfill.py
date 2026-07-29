"""Dopárování nenapárovaných ingrediencí bez LLM (slovník + fuzzy match).

Rychlá, deterministická část párování: slovník aliasů + fuzzy match proti
názvům surovin. Všechno, co tudy neprojde, patří dávkovému LLM dopárování
(`llm_match.py`) a katalogu rozhodnutí (`match_decision`) – TAM se rozhoduje
o nejasných případech, tady se nic nevymýšlí.

Dřív měl backfill vlastní LLM fázi (parsování + tvorba nových surovin s
výživou odhadnutou modelem), která obcházela katalog rozhodnutí a používala
jinou normalizaci klíče (`_clean_name`) než zbytek appky (`make_lookup_key`).
Dva nekompatibilní slovníky a tichá tvorba surovin s vymyšlenou výživou –
obojí odstraněno. Aliasy z fuzzy matche se teď ukládají s `lookup_key`,
takže je vidí i llm_match (slovníkový sweep) a naopak.

Škálovatelnost: řádky se zpracovávají po chunkách podle id (žádné "všechno
do paměti" – jen slovník a názvy surovin, ty jsou malé).
"""
from __future__ import annotations

import logging
import threading
import time

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Ingredient, IngredientAlias, Recipe, RecipeIngredient
from .lookup import make_lookup_key
from .normalizer import _norm, is_section_header, parse_line_regex
from .nutrition import grams_for, kcal_for, recompute_recipe_kcal

log = logging.getLogger("kucharka.backfill")

# Práh fuzzy matche (token_set_ratio, 0–100). Historicky 82 – ověřené na
# produkčních datech; přísnější případy nechá LLM fázi.
FUZZY_CUTOFF = 82
CHUNK = 1000

_lock = threading.Lock()
_state: dict = {
    "running": False, "phase": None, "done": 0, "total": 0,
    "matched": 0, "created": 0, "finished_at": None, "error": None,
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def _try_start() -> bool:
    """Atomická pojistka proti dvojímu běhu (scheduler + ruční spuštění)."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        return True


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def stats() -> dict:
    db = SessionLocal()
    try:
        return {
            "rows_total": db.scalar(select(func.count(RecipeIngredient.id))) or 0,
            "rows_unmatched": db.scalar(
                select(func.count(RecipeIngredient.id)).where(
                    RecipeIngredient.ingredient_id.is_(None)
                )
            ) or 0,
            "recipes_total": db.scalar(select(func.count(Recipe.id))) or 0,
            "recipes_unmatched": db.scalar(
                select(func.count(func.distinct(RecipeIngredient.recipe_id))).where(
                    RecipeIngredient.ingredient_id.is_(None)
                )
            ) or 0,
            "ingredients_total": db.scalar(select(func.count(Ingredient.id))) or 0,
        }
    finally:
        db.close()


def status() -> dict:
    with _lock:
        s = dict(_state)
    s.update(stats())
    return s


class _Matcher:
    """In-memory matcher: slovník aliasů + fuzzy proti názvům surovin."""

    def __init__(self, db: Session):
        self.db = db
        self.by_id: dict[int, Ingredient] = {}
        self.choices: dict[int, str] = {}
        # klíč → (ingredient_id | None, kind); None = non-food záznam
        self.alias_map: dict[str, tuple[int | None, str]] = {}
        for ing in db.scalars(select(Ingredient)).all():
            self.by_id[ing.id] = ing
            self.choices[ing.id] = _norm(ing.name_cs)
        for lookup_key, alias, iid, kind in db.execute(
            select(IngredientAlias.lookup_key, IngredientAlias.alias,
                   IngredientAlias.ingredient_id, IngredientAlias.kind)
        ).all():
            if lookup_key:
                self.alias_map[lookup_key] = (iid, kind or "food")
            if alias and alias not in self.alias_map:
                # legacy záznamy bez lookup_key
                self.alias_map[alias] = (iid, kind or "food")

    def match(self, key: str) -> tuple[Ingredient | None, bool]:
        """Vrátí (surovina | None, je_to_nonfood_zaznam)."""
        hit = self.alias_map.get(key)
        if hit is not None:
            iid, kind = hit
            if kind != "food" or not iid:
                return None, True
            return self.by_id.get(iid), False
        best = process.extractOne(
            key, self.choices, scorer=fuzz.token_set_ratio, score_cutoff=FUZZY_CUTOFF
        )
        if not best:
            return None, False
        iid = best[2]
        self.alias_map[key] = (iid, "food")
        self._save_alias(key, iid, best[1] / 100.0)
        return self.by_id.get(iid), False

    def _save_alias(self, key: str, iid: int, confidence: float) -> None:
        """Ulož nový fuzzy alias hned (vlastní commit), ať konflikt na unikátní
        klíč (souběžný zápis odjinud) nezahodí rozdělanou dávku řádků."""
        self.db.add(IngredientAlias(
            alias=key[:200], lookup_key=key[:200], ingredient_id=iid,
            kind="food", source="import", confidence=confidence, verified=False,
            hit_count=1,
        ))
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.execute(
                select(IngredientAlias.ingredient_id, IngredientAlias.kind)
                .where(IngredientAlias.lookup_key == key)
            ).first()
            if existing is not None:
                self.alias_map[key] = (existing[0], existing[1] or "food")
        except Exception as exc:  # noqa: BLE001 - např. moc dlouhý klíč
            self.db.rollback()
            log.warning("backfill: alias %r nešel uložit: %s", key[:80], exc)


def purge_headers(db: Session) -> int:
    """Smaže nenapárované řádky, které jsou jen nadpis skupiny ("Dále:",
    "Drobenka:", "Na vymazání a vysypání formy:"). Dřív jen ruční tlačítko
    v administraci – teď běží automaticky na začátku každého párování,
    ať se nadpisy nehromadí (v produkci jich čekaly tisíce)."""
    removed = 0
    last_id = 0
    while True:
        rows = db.execute(
            select(RecipeIngredient.id, RecipeIngredient.raw_text)
            .where(
                RecipeIngredient.ingredient_id.is_(None),
                RecipeIngredient.id > last_id,
            )
            .order_by(RecipeIngredient.id)
            .limit(CHUNK)
        ).all()
        if not rows:
            break
        for row_id, raw_text in rows:
            last_id = row_id
            if is_section_header(raw_text or ""):
                obj = db.get(RecipeIngredient, row_id)
                if obj is not None:
                    db.delete(obj)
                    removed += 1
        db.commit()
    if removed:
        log.info("purge nadpisů: smazáno %s řádků", removed)
    return removed


def _apply(row: RecipeIngredient, ing: Ingredient):
    amount, unit, _name = parse_line_regex(row.raw_text or "")
    if row.amount is None and amount is not None:
        row.amount = amount
    if not row.unit and unit:
        row.unit = unit
    row.ingredient_id = ing.id
    row.grams = grams_for(row.amount, row.unit, ing)
    row.kcal = kcal_for(row.grams, ing)


def backfill(create_missing: bool = False, chunk: int = CHUNK) -> dict:
    """Jeden běh: slovník + fuzzy nad všemi nenapárovanými řádky, po chunkách.

    `create_missing` je ponecháno kvůli API kompatibilitě, ale nic nedělá –
    tvorbu surovin řeší výhradně LLM dopárování přes katalog rozhodnutí.
    """
    if not _try_start():
        log.info("backfill: už běží (spuštěno odjinud) – tenhle běh přeskakuji.")
        return status()
    db = SessionLocal()
    affected: set[int] = set()
    matched = 0
    try:
        _set(phase="headers", done=0, total=0,
             matched=0, created=0, finished_at=None, error=None)
        purge_headers(db)
        total = db.scalar(
            select(func.count(RecipeIngredient.id)).where(
                RecipeIngredient.ingredient_id.is_(None)
            )
        ) or 0
        _set(phase="fuzzy", done=0, total=total,
             matched=0, created=0, finished_at=None, error=None)

        m = _Matcher(db)
        done = 0
        last_id = 0
        while True:
            rows = db.scalars(
                select(RecipeIngredient)
                .where(
                    RecipeIngredient.ingredient_id.is_(None),
                    RecipeIngredient.id > last_id,
                )
                .order_by(RecipeIngredient.id)
                .limit(chunk)
            ).all()
            if not rows:
                break
            for row in rows:
                last_id = row.id
                done += 1
                key = make_lookup_key(row.raw_text or "")
                if not key:
                    continue
                ing, nonfood = m.match(key)
                if nonfood or ing is None:
                    continue
                _apply(row, ing)
                matched += 1
                affected.add(row.recipe_id)
            db.commit()
            _set(done=done, matched=matched)

        # --- přepočet kalorií dotčených receptů ---
        _set(phase="kcal")
        for i, rid in enumerate(sorted(affected), 1):
            recipe = db.get(Recipe, rid)
            if recipe is not None:
                recompute_recipe_kcal(recipe)
            if i % 200 == 0:
                db.commit()
        db.commit()
        log.info("backfill hotovo: napárováno %s řádků v %s receptech",
                 matched, len(affected))
    except Exception as exc:  # noqa: BLE001
        log.exception("backfill selhal: %s", exc)
        db.rollback()
        _set(error=str(exc)[:500])
    finally:
        _set(running=False, phase=None, finished_at=time.time())
        db.close()
    return status()


def backfill_async(create_missing: bool = False) -> bool:
    if is_running():
        return False
    threading.Thread(
        target=backfill, kwargs={"create_missing": create_missing}, daemon=True
    ).start()
    return True
