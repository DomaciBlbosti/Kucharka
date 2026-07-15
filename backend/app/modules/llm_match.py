"""LLM batch matching pro nenamatchnuté suroviny (recipe_ingredient.ingredient_id IS NULL).

Cíl: dorovnat to, co slovník + fuzzy match nezachytily — typicky cizojazyčné
suroviny (anglicky, italsky, indicky) na cizích webech.

Postup:
  1. Posbírej všechny nematchnuté `recipe_ingredient` řádky (bez ohledu na
     `Recipe.enrichment_status` — původně filtrováno na 'manual_review', ale
     ten stav v produkci nikdy nenastává, takže fronta byla trvale prázdná;
     stejná definice jako `backfill.py` používá).
  2. Deduplikuj podle `lookup_key` — stejný "chicken breast" se ptáme jen jednou.
  3. Batch 30–50 surovin → 1 LLM volání s kontextem (seznam ingredient_id+name_cs).
  4. Validuj odpověď, ulož platné mapování do `ingredient_alias` (source='llm').
  5. Re-enrichment dotčených receptů (slovník teď zná → projde).

Opt-in přes `LLM_MATCH_ENABLED=true`. Default off — abychom se neopírali
o Ollamu, dokud uživatel explicitně neřekne.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime

import httpx
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, IngredientAlias, Recipe, RecipeIngredient
from . import ingredient_embed
from .lookup import make_lookup_key

log = logging.getLogger("kucharka.llm_match")

_lock = threading.Lock()
_state: dict = {
    "running": False, "done": 0, "total": 0,
    "applied": 0, "rejected": 0, "nonfood": 0,
    "finished_at": None,
}


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def status() -> dict:
    with _lock:
        s = dict(_state)
    db = SessionLocal()
    try:
        s["unmatched"] = db.scalar(
            select(func.count(RecipeIngredient.id))
            .where(RecipeIngredient.ingredient_id.is_(None))
        ) or 0
    finally:
        db.close()
    return s


# ─── Konfigurace ─────────────────────────────────────────────────────────────

DEFAULT_BATCH_SIZE = 40           # surovin na jedno LLM volání
DEFAULT_MIN_CONFIDENCE = 0.7      # méně = zahodit (nesnižovat kvalitu slovníku)
DEFAULT_INGREDIENT_LIST_SIZE = 250  # top N kandidátů, kteří se posílají LLM v promptu


# ─── Sestavení kontextu ──────────────────────────────────────────────────────

def _build_ingredient_catalog(db: Session, limit: int = DEFAULT_INGREDIENT_LIST_SIZE) -> list[tuple[int, str]]:
    """Top N ingrediencí pro prompt. Preferuj často používané (vysoký hit_count v aliasech),
    fallback na low-id (typicky seed surovin).
    """
    # Skóre = sum hit_count přes aliasy
    hit_sub = (
        select(IngredientAlias.ingredient_id, func.coalesce(func.sum(IngredientAlias.hit_count), 0).label("total_hits"))
        .where(IngredientAlias.ingredient_id.is_not(None))
        .group_by(IngredientAlias.ingredient_id)
        .subquery()
    )
    rows = db.execute(
        select(Ingredient.id, Ingredient.name_cs, func.coalesce(hit_sub.c.total_hits, 0).label("hits"))
        .outerjoin(hit_sub, Ingredient.id == hit_sub.c.ingredient_id)
        .order_by(func.coalesce(hit_sub.c.total_hits, 0).desc(), Ingredient.id.asc())
        .limit(limit)
    ).all()
    return [(r.id, r.name_cs) for r in rows if r.name_cs]


def _collect_unmatched_raw_texts(db: Session) -> dict[str, list[int]]:
    """Vrátí mapping `raw_text → list[recipe_id]` pro nematchnuté řádky.

    Původně filtrovalo na `Recipe.enrichment_status == 'manual_review'`, ale
    ten stav v praxi nastavuje jen `enrichment.py` worker, který v produkci
    neběží – fronta tak byla trvale prázdná. Skutečný backlog nenamatchnutých
    řádků (viz /api/maintenance/match-status → rows_unmatched) drží
    `backfill.py`, který jede nad VŠEMI řádky s `ingredient_id IS NULL` bez
    ohledu na stav receptu. Sjednoceno na stejnou definici, ať `llm_match`
    a `backfill` míří na tu samou frontu.
    """
    rows = db.execute(
        select(RecipeIngredient.id, RecipeIngredient.raw_text, RecipeIngredient.recipe_id)
        .where(
            RecipeIngredient.ingredient_id.is_(None),
            RecipeIngredient.raw_text.is_not(None),
        )
    ).all()
    by_text: dict[str, set[int]] = defaultdict(set)
    for ri_id, raw_text, recipe_id in rows:
        if raw_text and raw_text.strip():
            by_text[raw_text.strip()].add(recipe_id)
    return {t: sorted(rids) for t, rids in by_text.items()}


# ─── LLM volání ──────────────────────────────────────────────────────────────

# JSON schéma odpovědi – posílá se v `format`, Ollama pak vynutí strukturu
# přes constrained sampling (ne jen "je to nějaký JSON", ale přesně tohle).
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                    "ingredient_id": {"type": ["integer", "null"]},
                    "category": {
                        "type": "string",
                        "enum": ["food", "equipment", "garnish", "packaging", "unknown"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["input", "category", "confidence"],
            },
        }
    },
    "required": ["items"],
}

_PROMPT_HEADER = """Jsi expert na český kulinář. Tvým úkolem je přiřadit suroviny z receptů (často cizojazyčné) k odpovídajícím záznamům v české databázi surovin.

Pravidla:
- ingredient_id MUSÍ být ID z databáze níže, nebo null pokud nic nepasuje.
- category: "food" pro suroviny; "equipment" (forma, lžíce, struhadlo); "garnish" (na ozdobu); "packaging" (folie, alobal); "unknown" jinak.
- confidence: 0.9+ = jistá shoda; 0.7-0.9 = pravděpodobná; <0.7 = NULL ingredient_id.
- Cizojazyčné názvy přelož: "chicken breast" → kuřecí prsa; "soy sauce" → sójová omáčka; "cilantro" → koriandr.
- Při nejistotě raději null než hádat.

Příklady chování:
- "chicken breast" → přelož, najdi "kuřecí prsa" v databázi, category="food", confidence=0.95
- "silikonová forma na muffiny" → category="equipment", ingredient_id=null, confidence=0.9
- "trochu lásky :)" → category="unknown", ingredient_id=null, confidence=0.0

Databáze surovin (id: name):
"""


def _make_prompt(catalog: list[tuple[int, str]], inputs: list[str]) -> str:
    catalog_str = "\n".join(f"{cid}: {name}" for cid, name in catalog)
    inputs_str = "\n".join(f"- {t}" for t in inputs)
    return f"{_PROMPT_HEADER}{catalog_str}\n\nSuroviny k přiřazení:\n{inputs_str}\n"


def _call_ollama(prompt: str, model: str | None = None) -> dict | None:
    """Vrátí parsed JSON odpověď nebo None při chybě."""
    if not settings.ollama_url:
        log.warning("OLLAMA_URL není nastaven — LLM matching nelze spustit.")
        return None
    use_model = model or settings.llm_match_model or settings.ollama_fast_model
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": use_model,
                "prompt": prompt,
                "stream": False,
                "format": _RESPONSE_SCHEMA,  # vynucené schéma, ne jen "je to JSON"
                "think": False,
                "keep_alive": settings.ollama_keep_alive,
                "options": {
                    "temperature": settings.llm_match_temperature,
                    "num_ctx": settings.llm_match_num_ctx,
                },
            },
            timeout=180,  # batch může být pomalý, dej mu 3 min
        )
        r.raise_for_status()
        raw = r.json().get("response", "")
        return json.loads(raw)
    except httpx.HTTPError as exc:
        log.warning("LLM volání selhalo: %s", exc)
        return None
    except json.JSONDecodeError as exc:
        log.warning("LLM odpověď není validní JSON: %s; raw=%.300s", exc, raw if 'raw' in dir() else '?')
        return None


# ─── Aplikace výsledků ───────────────────────────────────────────────────────

def _apply_matches(
    db: Session,
    matches: list[dict],
    raw_text_to_recipes: dict[str, list[int]],
    valid_ingredient_ids: set[int],
    min_confidence: float,
) -> dict:
    """Pro každý match (input → ingredient_id) ulož alias a vrať statistiky.

    Vrátí dict {applied, rejected, nonfood, recipes_touched}.
    """
    applied = rejected = nonfood = 0
    recipes_touched: set[int] = set()

    for m in matches:
        raw = (m.get("input") or "").strip()
        if not raw or raw not in raw_text_to_recipes:
            continue
        category = (m.get("category") or "food").lower()
        confidence = float(m.get("confidence") or 0)
        ing_id = m.get("ingredient_id")

        if confidence < min_confidence and category == "food":
            rejected += 1
            continue

        if category != "food":
            # Non-food → uložit do slovníku jako equipment/garnish/packaging,
            # ingredient_id zůstane NULL. Filter v ingestu pak řádek skip.
            _upsert_alias(db, raw, ingredient_id=None, kind=category,
                         source="llm", confidence=confidence)
            nonfood += 1
            recipes_touched.update(raw_text_to_recipes[raw])
            continue

        if ing_id is None or ing_id not in valid_ingredient_ids:
            rejected += 1
            continue

        _upsert_alias(db, raw, ingredient_id=ing_id, kind="food",
                     source="llm", confidence=confidence)
        applied += 1
        recipes_touched.update(raw_text_to_recipes[raw])

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        # Poslední pojistka – s db.flush() v _upsert_alias by tohle nemělo
        # nastat pro duplicity v rámci dávky, ale kdyby přece (souběh,
        # cizí klíč apod.), ať aspoň nepadá celý běh a předchozí dávky
        # zůstanou uložené.
        log.error("commit dávky selhal, rollback: %s", exc)
        db.rollback()
        return {"applied": 0, "rejected": applied + rejected + nonfood, "nonfood": 0,
                "recipes_touched": 0, "recipe_ids": []}
    return {
        "applied": applied,
        "rejected": rejected,
        "nonfood": nonfood,
        "recipes_touched": len(recipes_touched),
        "recipe_ids": sorted(recipes_touched),
    }


def _upsert_alias(
    db: Session,
    raw_text: str,
    *,
    ingredient_id: int | None,
    kind: str,
    source: str,
    confidence: float,
) -> None:
    """Vlož nebo updatuj alias. Klíč unikátnosti: lookup_key (preferované) nebo alias."""
    from app.modules.lookup import make_lookup_key as mlk
    lookup_key = mlk(raw_text)
    clean_alias = raw_text.lower().strip()[:200]

    existing = None
    if lookup_key:
        existing = db.scalar(
            select(IngredientAlias).where(IngredientAlias.lookup_key == lookup_key)
        )
    if existing is None:
        existing = db.scalar(
            select(IngredientAlias).where(IngredientAlias.alias == clean_alias)
        )

    if existing is not None:
        # Aktualizuj, pokud LLM přinesl jistější odpověď
        if existing.source == "llm" and (existing.confidence or 0) < confidence:
            existing.ingredient_id = ingredient_id
            existing.kind = kind
            existing.confidence = confidence
            existing.last_seen_at = datetime.utcnow()
        # Jinak nech — manuální / import přepisovat nebudeme
        return

    db.add(IngredientAlias(
        alias=clean_alias,
        lookup_key=lookup_key or None,
        ingredient_id=ingredient_id,
        kind=kind,
        source=source,
        confidence=confidence,
        verified=False,
        hit_count=0,
        last_seen_at=datetime.utcnow(),
    ))
    # Flush (ne commit) hned – bez tohohle další položka ve STEJNÉ dávce se
    # stejným lookup_key (např. "1 ks sojový suk" a "2 ks sojový suk" →
    # stejný normalizovaný klíč) neuvidí tenhle pending insert, zkusí vložit
    # znovu, a IntegrityError na konci dávky odrolluje úplně všechno –
    # včetně matchů, které jinak v pořádku prošly.
    db.flush()


# ─── Re-enrichment dotčených receptů ─────────────────────────────────────────

def _reenrich_recipes(db: Session, recipe_ids: list[int]) -> dict:
    """Po doplnění slovníku znovu projeď enrichment u dotčených receptů."""
    if not recipe_ids:
        return {"reenriched": 0}
    from . import enrichment
    ing_by_name, ing_names = enrichment._build_ingredient_index(db)
    touched = 0
    for rid in recipe_ids:
        recipe = db.get(Recipe, rid)
        if recipe is None:
            continue
        try:
            enrichment.enrich_recipe(db, recipe, ing_by_name=ing_by_name, ing_names=ing_names)
            db.commit()
            touched += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("re-enrich recept %s: %s", rid, exc)
            db.rollback()
    return {"reenriched": touched}


# ─── Hlavní vstup workeru ────────────────────────────────────────────────────

def process_batch(batch_size: int | None = None) -> dict:
    """Jedno spuštění workeru. Vrátí statistiky."""
    if not settings.llm_match_enabled:
        return {"skipped": "llm_match disabled"}
    if not settings.ollama_url:
        return {"skipped": "ollama not configured"}

    bs = batch_size or settings.llm_match_batch_size or DEFAULT_BATCH_SIZE
    min_conf = settings.llm_match_min_confidence

    db = SessionLocal()
    try:
        raw_text_to_recipes = _collect_unmatched_raw_texts(db)
        if not raw_text_to_recipes:
            return {"unmatched": 0}

        # Pojistka proti opakovanému ptaní LLM na to samé:
        # vyhoď raw_texty, jejichž lookup_key už ve slovníku JE.
        keys_in_dict = set(db.scalars(
            select(IngredientAlias.lookup_key).where(IngredientAlias.lookup_key.is_not(None))
        ).all())
        skipped_already_in_dict = 0
        filtered = {}
        for raw, rids in raw_text_to_recipes.items():
            if make_lookup_key(raw) in keys_in_dict:
                skipped_already_in_dict += 1
                continue
            filtered[raw] = rids
        raw_text_to_recipes = filtered

        if not raw_text_to_recipes:
            return {"unmatched": 0, "skipped_already_in_dict": skipped_already_in_dict}

        log.info(
            "LLM match: %s unikátních surovin (po deduplikaci), batch=%s",
            len(raw_text_to_recipes), bs,
        )

        static_catalog = _build_ingredient_catalog(db)
        valid_ids = {cid for cid, _ in static_catalog}
        # Plus všechny existující ingredient_id (i mimo top N) — LLM mohl trefit i hlubší ID
        all_ids = set(db.scalars(select(Ingredient.id)).all())
        valid_ids.update(all_ids)

        inputs = list(raw_text_to_recipes.keys())
        totals = {
            "applied": 0, "rejected": 0, "nonfood": 0,
            "recipes_touched": 0, "batches": 0,
            "skipped_already_in_dict": skipped_already_in_dict,
        }
        all_touched_recipes: set[int] = set()
        with _lock:
            _state.update(total=len(inputs), done=0)

        for start in range(0, len(inputs), bs):
            chunk = inputs[start:start + bs]
            # Dynamický katalog (embeddingy) – jen sémanticky relevantní kandidáti
            # pro tuhle dávku, ne statický top-N podle popularity. Fallback na
            # statický katalog, dokud neproběhl `ingredient_embed.reindex()`.
            dynamic_catalog = ingredient_embed.candidates_for_batch(db, chunk, k=20)
            if not dynamic_catalog:
                log.warning(
                    "dávka %s: dynamický katalog nedostupný (embeddingy?), fallback na statický top-N",
                    totals["batches"] + 1,
                )
            catalog = dynamic_catalog or static_catalog
            prompt = _make_prompt(catalog, chunk)
            resp = _call_ollama(prompt)
            totals["batches"] += 1
            if resp is None:
                totals["rejected"] += len(chunk)
            else:
                items = resp.get("items", []) if isinstance(resp, dict) else []
                if not items:
                    totals["rejected"] += len(chunk)
                else:
                    stats = _apply_matches(db, items, raw_text_to_recipes, valid_ids, min_conf)
                    totals["applied"] += stats["applied"]
                    totals["rejected"] += stats["rejected"]
                    totals["nonfood"] += stats["nonfood"]
                    all_touched_recipes.update(stats["recipe_ids"])
            with _lock:
                _state.update(
                    done=min(start + bs, len(inputs)),
                    applied=totals["applied"], rejected=totals["rejected"],
                    nonfood=totals["nonfood"],
                )

        # Re-enrichment receptů, kterých se LLM matche dotkly
        re = _reenrich_recipes(db, list(all_touched_recipes))
        totals["recipes_touched"] = len(all_touched_recipes)
        totals["reenriched"] = re["reenriched"]
        log.info("LLM match hotov: %s", totals)
        return totals
    finally:
        db.close()


def _run_bg(batch_size: int | None) -> None:
    try:
        process_batch(batch_size=batch_size)
    finally:
        with _lock:
            _state.update(running=False, finished_at=time.time())


def process_batch_async(batch_size: int | None = None) -> bool:
    """Spustí process_batch na pozadí. Vrátí False, pokud už něco běží."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, total=0, applied=0, rejected=0,
                       nonfood=0, finished_at=None)
    threading.Thread(target=_run_bg, args=(batch_size,), daemon=True).start()
    return True


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
    print(process_batch())
