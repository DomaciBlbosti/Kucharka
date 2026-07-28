"""LLM batch matching pro nenamatchnuté suroviny (recipe_ingredient.ingredient_id IS NULL).

Cíl: dorovnat to, co slovník + fuzzy match nezachytily — typicky cizojazyčné
suroviny (anglicky, italsky, indicky) na cizích webech.

Postup (jeden běh):
  0. Slovníkový sweep: nenapárované řádky, jejichž `lookup_key` už ve slovníku
     JE (z dřívějších běhů / ručních rozhodnutí), se napárují rovnou bez LLM.
     Tím se každý běh nejdřív "dorovná" a LLM řeší jen skutečně nové texty.
  1. Zbytek se deduplikuje podle `lookup_key` (ne podle surového textu — "1 ks
     sojový suk" a "2 ks sojové suky" je jedna otázka, ne dvě).
  2. Přeskočí se položky, o kterých už existuje záznam v `match_decision`
     (katalog rozhodnutí) — ať se LLM neptá opakovaně na totéž. Výjimka:
     status='error' se zkouší znovu, max MAX_ATTEMPTS pokusů.
  3. Dávka ~40 položek → 1 LLM volání. Položky jsou číslované a odpověď se
     páruje podle indexu `i` — NE podle doslovného echa vstupního textu,
     které modely běžně "opravují" (překlad, diakritika) a match tak padal.
  4. KAŽDÝ výsledek se uloží do `match_decision`: jistý match se aplikuje
     (alias + napárování řádků), nejistý čeká jako 'suggested', bez kandidáta
     jako 'no_match', non-food jako 'nonfood', chyba jako 'error'. Nic se
     tiše nezahazuje — vše je vidět v administraci a dá se ručně dořešit.
  5. Na konci se přepočítají kalorie dotčených receptů.

Díky per-batch commitům je běh kdykoliv přerušitelný — už rozhodnuté položky
se při dalším běhu přeskočí a slovníkový sweep dopáruje, co se nestihlo.

Opt-in přes `LLM_MATCH_ENABLED=true`. LLM volání jde přes `llmclient`
(lokální Ollama, nebo komerční OpenAI-kompatibilní API podle LLM_PROVIDER).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, IngredientAlias, MatchDecision, Recipe, RecipeIngredient
from . import ingredient_embed, llmclient
from .lookup import make_lookup_key
from .nutrition import grams_for, kcal_for, recompute_recipe_kcal

log = logging.getLogger("kucharka.llm_match")

_lock = threading.Lock()
_state: dict = {
    "running": False, "phase": None, "done": 0, "total": 0,
    "embed_done": 0, "embed_total": 0,
    "dict_applied": 0, "applied": 0, "suggested": 0, "no_match": 0,
    "nonfood": 0, "errors": 0,
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
        # zpětná kompatibilita pro starší UI: zamítnuto = návrhy + bez shody
        s["rejected"] = (s.get("suggested") or 0) + (s.get("no_match") or 0)
    finally:
        db.close()
    return s


# ─── Konfigurace ─────────────────────────────────────────────────────────────

DEFAULT_BATCH_SIZE = 40           # surovin na jedno LLM volání
DEFAULT_MIN_CONFIDENCE = 0.7      # méně = jen návrh do katalogu, ne automatika
DEFAULT_INGREDIENT_LIST_SIZE = 250  # top N kandidátů pro statický katalog
MAX_ATTEMPTS = 3                  # kolikrát zkoušet položku po chybě LLM

# Finální stavy — položky s nimi se už LLM znovu neposílají.
_SETTLED_STATUSES = ("applied", "nonfood", "suggested", "no_match", "ignored")


# ─── Sběr a deduplikace vstupů ───────────────────────────────────────────────

class _Group:
    """Všechny nenapárované řádky se stejným lookup_key."""

    __slots__ = ("key", "sample", "row_ids", "recipe_ids")

    def __init__(self, key: str):
        self.key = key
        self.sample: str = ""
        self.row_ids: list[int] = []
        self.recipe_ids: set[int] = set()

    def add(self, row_id: int, raw_text: str, recipe_id: int) -> None:
        self.row_ids.append(row_id)
        self.recipe_ids.add(recipe_id)
        # nejkratší text jako reprezentant — bývá nejčistší tvar
        if not self.sample or len(raw_text) < len(self.sample):
            self.sample = raw_text


def _collect_groups(db: Session) -> dict[str, _Group]:
    """Nenapárované řádky seskupené podle lookup_key. Prázdné klíče se vynechají
    (text je jen množství/smajlík — není co párovat)."""
    rows = db.execute(
        select(RecipeIngredient.id, RecipeIngredient.raw_text, RecipeIngredient.recipe_id)
        .where(
            RecipeIngredient.ingredient_id.is_(None),
            RecipeIngredient.raw_text.is_not(None),
        )
    ).all()
    groups: dict[str, _Group] = {}
    for row_id, raw_text, recipe_id in rows:
        raw = (raw_text or "").strip()
        if not raw:
            continue
        key = make_lookup_key(raw)
        if not key:
            continue
        g = groups.get(key)
        if g is None:
            g = groups[key] = _Group(key)
        g.add(row_id, raw, recipe_id)
    return groups


# ─── Fáze 0: aplikace existujícího slovníku ──────────────────────────────────

def _apply_rows(db: Session, row_ids: list[int], ing: Ingredient) -> set[int]:
    """Napáruje surovinu na dané řádky (jen ty stále nenapárované) a dopočítá
    grams/kcal. Vrátí recipe_id dotčených receptů. Necommituje."""
    from .enrichment import _parse_amount_unit

    touched: set[int] = set()
    rows = db.scalars(
        select(RecipeIngredient).where(RecipeIngredient.id.in_(row_ids))
    ).all()
    for r in rows:
        if r.ingredient_id is not None:
            continue
        if r.amount is None and r.unit is None:
            r.amount, r.unit = _parse_amount_unit(r.raw_text or "")
        r.ingredient_id = ing.id
        r.grams = grams_for(r.amount, r.unit, ing)
        r.kcal = kcal_for(r.grams, ing)
        touched.add(r.recipe_id)
    return touched


def _apply_dictionary(db: Session, groups: dict[str, _Group]) -> tuple[dict, set[int]]:
    """Napáruje skupiny, jejichž klíč už ve slovníku je (food i non-food).
    Odebere je z `groups`. Vrátí (statistiky, dotčené recepty)."""
    alias_rows = db.execute(
        select(IngredientAlias.lookup_key, IngredientAlias.ingredient_id, IngredientAlias.kind)
        .where(IngredientAlias.lookup_key.is_not(None))
    ).all()
    alias_map = {key: (iid, kind) for key, iid, kind in alias_rows}

    stats = {"dict_applied": 0, "dict_nonfood": 0}
    affected: set[int] = set()
    ing_cache: dict[int, Ingredient | None] = {}
    pending_commit = 0

    for key in list(groups.keys()):
        hit = alias_map.get(key)
        if hit is None:
            continue
        iid, kind = hit
        g = groups.pop(key)
        if kind == "food" and iid:
            ing = ing_cache.get(iid)
            if ing is None and iid not in ing_cache:
                ing = ing_cache[iid] = db.get(Ingredient, iid)
            if ing is None:
                continue  # alias na smazanou surovinu
            affected |= _apply_rows(db, g.row_ids, ing)
            stats["dict_applied"] += len(g.row_ids)
            pending_commit += len(g.row_ids)
            if pending_commit >= 1000:
                db.commit()
                pending_commit = 0
        else:
            # non-food: řádky zůstávají bez suroviny záměrně
            stats["dict_nonfood"] += len(g.row_ids)
    db.commit()
    return stats, affected


# ─── Katalog rozhodnutí ──────────────────────────────────────────────────────

def _upsert_decision(
    db: Session,
    key: str,
    sample: str,
    *,
    status: str,
    category: str | None = None,
    ingredient_id: int | None = None,
    confidence: float | None = None,
    model: str | None = None,
    occurrences: int | None = None,
    error: str | None = None,
    bump_attempts: bool = False,
) -> MatchDecision:
    d = db.scalar(select(MatchDecision).where(MatchDecision.lookup_key == key))
    if d is None:
        d = MatchDecision(lookup_key=key, sample_text=(sample or key)[:400],
                          status=status, attempts=0, occurrences=0)
        db.add(d)
    d.status = status
    d.category = category
    d.ingredient_id = ingredient_id
    d.confidence = confidence
    d.model = model
    d.error = error
    if occurrences is not None:
        d.occurrences = occurrences
    if bump_attempts:
        d.attempts = (d.attempts or 0) + 1
    d.updated_at = datetime.utcnow()
    db.flush()
    return d


def decisions_summary(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(MatchDecision.status, func.count(MatchDecision.id))
        .group_by(MatchDecision.status)
    ).all()
    return {s: c for s, c in rows}


# ─── Sestavení promptu ───────────────────────────────────────────────────────

def _build_ingredient_catalog(db: Session, limit: int = DEFAULT_INGREDIENT_LIST_SIZE) -> list[tuple[int, str]]:
    """Statický top-N katalog (fallback, když nejsou embeddingy): preferuj
    často používané suroviny (vysoký hit_count v aliasech)."""
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


# JSON schéma odpovědi. Párování podle indexu `i` (pořadí v dávce), ne podle
# echa vstupního textu — modely echo běžně mění a match pak selhával.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "ingredient_id": {"type": ["integer", "null"]},
                    "category": {
                        "type": "string",
                        "enum": ["food", "equipment", "garnish", "packaging", "unknown"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["i", "category", "confidence"],
            },
        }
    },
    "required": ["items"],
}

_PROMPT_HEADER = """Jsi expert na české kulinářství. Tvým úkolem je přiřadit suroviny z receptů (často cizojazyčné) k odpovídajícím záznamům v české databázi surovin.

Pravidla:
- Pro KAŽDOU očíslovanou položku vrať právě jeden objekt s jejím indexem "i".
- ingredient_id MUSÍ být ID z databáze níže, nebo null pokud nic nepasuje.
- category: "food" pro suroviny; "equipment" (forma, lžíce, struhadlo); "garnish" (na ozdobu); "packaging" (folie, alobal); "unknown" jinak.
- confidence: 0.9+ = jistá shoda; 0.7-0.9 = pravděpodobná; pod 0.7 = nejistá.
- Cizojazyčné názvy přelož: "chicken breast" → kuřecí prsa; "soy sauce" → sójová omáčka; "cilantro" → koriandr.
- Při nejistotě dej nižší confidence, nehádej.

Příklady chování:
- "chicken breast" → najdi "kuřecí prsa" v databázi, category="food", confidence=0.95
- "silikonová forma na muffiny" → category="equipment", ingredient_id=null, confidence=0.9
- "trochu lásky :)" → category="unknown", ingredient_id=null, confidence=0.0

Databáze surovin (id: name):
"""


def _make_prompt(catalog: list[tuple[int, str]], inputs: list[str]) -> str:
    catalog_str = "\n".join(f"{cid}: {name}" for cid, name in catalog)
    inputs_str = "\n".join(f"{i}: {t}" for i, t in enumerate(inputs))
    return f"{_PROMPT_HEADER}{catalog_str}\n\nSuroviny k přiřazení (i: text):\n{inputs_str}\n"


def _call_llm(prompt: str) -> dict | None:
    return llmclient.structured_json(
        prompt,
        schema=_RESPONSE_SCHEMA,
        timeout=180,  # batch může být pomalý, dej mu 3 min
        temperature=settings.llm_match_temperature,
        num_ctx=settings.llm_match_num_ctx,
        ollama_model=settings.llm_match_model or settings.ollama_fast_model,
    )


# ─── Zpracování odpovědi jedné dávky ─────────────────────────────────────────

def _upsert_alias(
    db: Session,
    raw_text: str,
    *,
    lookup_key: str | None = None,
    ingredient_id: int | None,
    kind: str,
    source: str,
    confidence: float,
    verified: bool = False,
) -> None:
    """Vlož nebo updatuj alias. Klíč unikátnosti: lookup_key (preferované) nebo alias."""
    key = lookup_key if lookup_key is not None else make_lookup_key(raw_text)
    clean_alias = raw_text.lower().strip()[:200]

    existing = None
    if key:
        existing = db.scalar(
            select(IngredientAlias).where(IngredientAlias.lookup_key == key)
        )
    if existing is None:
        existing = db.scalar(
            select(IngredientAlias).where(IngredientAlias.alias == clean_alias)
        )

    if existing is not None:
        if source == "manual":
            # ruční rozhodnutí přepisuje vždy
            existing.ingredient_id = ingredient_id
            existing.kind = kind
            existing.source = source
            existing.confidence = confidence
            existing.verified = True
            existing.verified_at = datetime.utcnow()
            existing.last_seen_at = datetime.utcnow()
        elif existing.source == "llm" and not existing.verified and (existing.confidence or 0) < confidence:
            # LLM přepisuje jen vlastní, neověřené, méně jisté záznamy
            existing.ingredient_id = ingredient_id
            existing.kind = kind
            existing.confidence = confidence
            existing.last_seen_at = datetime.utcnow()
        return

    db.add(IngredientAlias(
        alias=clean_alias,
        lookup_key=key or None,
        ingredient_id=ingredient_id,
        kind=kind,
        source=source,
        confidence=confidence,
        verified=verified,
        verified_at=datetime.utcnow() if verified else None,
        hit_count=0,
        last_seen_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    ))
    # Flush hned – ať případný souběžný duplikát neodrolluje celou dávku.
    db.flush()


def _process_response(
    db: Session,
    resp: dict | None,
    batch: list[_Group],
    valid_ids: set[int],
    min_conf: float,
    model_name: str,
) -> tuple[dict, set[int]]:
    """Zapíše rozhodnutí pro KAŽDOU položku dávky. Vrátí (statistiky, dotčené recepty)."""
    stats = {"applied": 0, "suggested": 0, "no_match": 0, "nonfood": 0, "errors": 0}
    affected: set[int] = set()

    by_index: dict[int, dict] = {}
    if isinstance(resp, dict):
        for it in resp.get("items", []):
            try:
                by_index[int(it.get("i"))] = it
            except (TypeError, ValueError):
                continue

    for idx, g in enumerate(batch):
        it = by_index.get(idx)
        occurrences = len(g.row_ids)
        if it is None:
            _upsert_decision(
                db, g.key, g.sample, status="error", model=model_name,
                occurrences=occurrences, bump_attempts=True,
                error="LLM volání selhalo nebo model položku vynechal",
            )
            stats["errors"] += 1
            continue

        category = (it.get("category") or "food").lower()
        try:
            confidence = float(it.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        ing_id = it.get("ingredient_id")
        if not isinstance(ing_id, int):
            ing_id = None

        if category != "food":
            _upsert_alias(db, g.sample, lookup_key=g.key, ingredient_id=None,
                          kind=category, source="llm", confidence=confidence)
            _upsert_decision(db, g.key, g.sample, status="nonfood", category=category,
                             confidence=confidence, model=model_name, occurrences=occurrences)
            stats["nonfood"] += 1
            continue

        if ing_id is None or ing_id not in valid_ids:
            _upsert_decision(
                db, g.key, g.sample, status="no_match", category="food",
                confidence=confidence, model=model_name, occurrences=occurrences,
                error=(f"model vrátil neexistující ingredient_id {ing_id}" if ing_id else None),
            )
            stats["no_match"] += 1
            continue

        if confidence < min_conf:
            _upsert_decision(db, g.key, g.sample, status="suggested", category="food",
                             ingredient_id=ing_id, confidence=confidence,
                             model=model_name, occurrences=occurrences)
            stats["suggested"] += 1
            continue

        ing = db.get(Ingredient, ing_id)
        if ing is None:
            stats["no_match"] += 1
            _upsert_decision(db, g.key, g.sample, status="no_match", category="food",
                             confidence=confidence, model=model_name, occurrences=occurrences)
            continue
        _upsert_alias(db, g.sample, lookup_key=g.key, ingredient_id=ing_id,
                      kind="food", source="llm", confidence=confidence)
        affected |= _apply_rows(db, g.row_ids, ing)
        _upsert_decision(db, g.key, g.sample, status="applied", category="food",
                         ingredient_id=ing_id, confidence=confidence,
                         model=model_name, occurrences=occurrences)
        stats["applied"] += 1

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 - poslední pojistka, ať jedna
        # rozbitá dávka neshodí celý běh; předchozí dávky jsou už commitnuté
        log.error("commit dávky selhal, rollback: %s", exc)
        db.rollback()
        return {"applied": 0, "suggested": 0, "no_match": 0, "nonfood": 0,
                "errors": len(batch)}, set()
    return stats, affected


# ─── Přepočet dotčených receptů ──────────────────────────────────────────────

def _finalize_recipes(db: Session, recipe_ids: set[int]) -> int:
    """Po napárování řádků přepočítá kcal/porci, celkovou váhu a kcal/100 g."""
    done = 0
    for i, rid in enumerate(sorted(recipe_ids), 1):
        recipe = db.get(Recipe, rid)
        if recipe is None:
            continue
        recompute_recipe_kcal(recipe)
        total_g = sum((ri.grams or 0.0) for ri in recipe.ingredients) or None
        recipe.total_weight_g = total_g
        if total_g and recipe.kcal_per_serving and recipe.servings:
            total_kcal = recipe.kcal_per_serving * recipe.servings
            recipe.kcal_per_100g = round(total_kcal / total_g * 100, 1)
        else:
            recipe.kcal_per_100g = None
        recipe.last_enriched_at = datetime.utcnow()
        done += 1
        if i % 200 == 0:
            db.commit()
    db.commit()
    return done


# ─── Ruční dořešení z katalogu rozhodnutí ────────────────────────────────────

def apply_manual_match(db: Session, decision: MatchDecision, ing: Ingredient) -> dict:
    """Aplikuje ruční rozhodnutí: alias (verified), napárování všech
    nenapárovaných řádků se stejným lookup_key, přepočet kalorií."""
    groups = _collect_groups(db)
    g = groups.get(decision.lookup_key)

    _upsert_alias(
        db, g.sample if g else decision.sample_text,
        lookup_key=decision.lookup_key, ingredient_id=ing.id,
        kind="food", source="manual", confidence=1.0, verified=True,
    )
    affected: set[int] = set()
    rows = 0
    if g is not None:
        affected = _apply_rows(db, g.row_ids, ing)
        rows = len(g.row_ids)

    decision.status = "applied"
    decision.category = "food"
    decision.ingredient_id = ing.id
    decision.confidence = 1.0
    decision.model = "manual"
    decision.error = None
    decision.occurrences = rows or decision.occurrences
    decision.updated_at = datetime.utcnow()
    db.commit()

    reenriched = _finalize_recipes(db, affected)
    return {"rows": rows, "recipes": reenriched, "ingredient_name": ing.name_cs}


# ─── Hlavní vstup workeru ────────────────────────────────────────────────────

def _try_start() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(
            running=True, phase="collecting", done=0, total=0,
            embed_done=0, embed_total=0,
            dict_applied=0, applied=0, suggested=0, no_match=0,
            nonfood=0, errors=0, finished_at=None,
        )
        return True


def process_batch(batch_size: int | None = None) -> dict:
    """Jedno spuštění workeru. Vrátí statistiky."""
    if not settings.llm_match_enabled:
        return {"skipped": "llm_match disabled"}
    err = llmclient.availability_error()
    if err:
        return {"skipped": err}
    if not _try_start():
        return {"skipped": "already running"}
    try:
        return _run()
    finally:
        with _lock:
            _state.update(running=False, phase=None, finished_at=time.time())


def _run(batch_size: int | None = None) -> dict:
    bs = batch_size or settings.llm_match_batch_size or DEFAULT_BATCH_SIZE
    min_conf = settings.llm_match_min_confidence
    model_name = llmclient.active_model(settings.llm_match_model or settings.ollama_fast_model)

    db = SessionLocal()
    try:
        groups = _collect_groups(db)
        totals: dict = {"unique_keys": len(groups)}
        if not groups:
            return totals

        # ─── Fáze 0: slovníkový sweep (bez LLM) ─────────────────────────
        with _lock:
            _state.update(phase="dictionary")
        dict_stats, affected = _apply_dictionary(db, groups)
        totals.update(dict_stats)
        with _lock:
            _state.update(dict_applied=dict_stats["dict_applied"])
        if dict_stats["dict_applied"]:
            log.info("slovníkový sweep: %s řádků napárováno bez LLM", dict_stats["dict_applied"])

        # ─── Přeskoč už rozhodnuté položky (katalog rozhodnutí) ─────────
        decisions = {
            d.lookup_key: d for d in db.scalars(select(MatchDecision)).all()
        }
        queue: list[_Group] = []
        skipped_decided = 0
        for key, g in groups.items():
            d = decisions.get(key)
            if d is None:
                queue.append(g)
            elif d.status == "error" and (d.attempts or 0) < MAX_ATTEMPTS:
                queue.append(g)
            else:
                skipped_decided += 1
                if d.occurrences != len(g.row_ids):
                    d.occurrences = len(g.row_ids)
        db.commit()
        totals["skipped_decided"] = skipped_decided

        if not queue:
            totals["reenriched"] = _finalize_recipes(db, affected)
            log.info("LLM match: nic nového k dotazování (%s už rozhodnuto). %s", skipped_decided, totals)
            return totals

        log.info(
            "LLM match: %s unikátních surovin k dotazování (model %s, batch=%s, %s přeskočeno jako rozhodnuté)",
            len(queue), model_name, bs, skipped_decided,
        )

        static_catalog = _build_ingredient_catalog(db)
        valid_ids = set(db.scalars(select(Ingredient.id)).all())

        chunks = [queue[i:i + bs] for i in range(0, len(queue), bs)]
        with _lock:
            _state.update(total=len(queue), done=0)

        # ─── Fáze 1: embeddingy pro dynamické katalogy (jen embed model) ─
        # Všechny najednou, ať se na jedné GPU nestřídá embed a chat model
        # při každé dávce (drahé reloady modelů).
        with _lock:
            _state.update(phase="embeddings", embed_done=0, embed_total=len(chunks))
        ingredient_embed.reset_circuit()
        catalogs: list[list[tuple[int, str]]] = []
        for i, chunk in enumerate(chunks):
            if i % 100 == 0 and i > 0:
                log.info("fáze 1 (embeddingy): %s/%s dávek zpracováno", i, len(chunks))
            with _lock:
                _state.update(embed_done=i)
            catalogs.append(
                ingredient_embed.candidates_for_batch(db, [g.sample for g in chunk], k=20)
            )

        # ─── Fáze 2: LLM volání (jen chat model) ────────────────────────
        with _lock:
            _state.update(phase="matching")
        run_stats = {"applied": 0, "suggested": 0, "no_match": 0, "nonfood": 0,
                     "errors": 0, "batches": 0}
        done_items = 0
        for idx, chunk in enumerate(chunks):
            catalog = catalogs[idx] or static_catalog
            prompt = _make_prompt(catalog, [g.sample for g in chunk])
            resp = _call_llm(prompt)
            run_stats["batches"] += 1
            stats, batch_affected = _process_response(
                db, resp, chunk, valid_ids, min_conf, model_name
            )
            for k in ("applied", "suggested", "no_match", "nonfood", "errors"):
                run_stats[k] += stats[k]
            affected |= batch_affected
            done_items += len(chunk)
            with _lock:
                _state.update(
                    done=done_items,
                    applied=run_stats["applied"], suggested=run_stats["suggested"],
                    no_match=run_stats["no_match"], nonfood=run_stats["nonfood"],
                    errors=run_stats["errors"],
                )
            if resp is None:
                log.warning(
                    "dávka %s selhala, čekám %ss na zotavení",
                    run_stats["batches"], settings.llm_match_failure_pause_s,
                )
                time.sleep(settings.llm_match_failure_pause_s)
            elif settings.llm_match_batch_pause_s and not settings.llm_api_enabled:
                # oddych jen pro lokální GPU; komerční API pauzy nepotřebuje
                time.sleep(settings.llm_match_batch_pause_s)

        # ─── Přepočet kalorií dotčených receptů ─────────────────────────
        with _lock:
            _state.update(phase="kcal")
        totals.update(run_stats)
        totals["recipes_touched"] = len(affected)
        totals["reenriched"] = _finalize_recipes(db, affected)
        log.info("LLM match hotov: %s", totals)
        return totals
    finally:
        db.close()


def _run_bg(batch_size: int | None) -> None:
    try:
        _run(batch_size)
    except Exception as exc:  # noqa: BLE001 - ať vlákno neumře potichu
        log.exception("LLM match běh selhal: %s", exc)
    finally:
        with _lock:
            _state.update(running=False, phase=None, finished_at=time.time())


def process_batch_async(batch_size: int | None = None) -> bool:
    """Spustí běh na pozadí. Vrátí False, pokud už něco běží."""
    if not _try_start():
        return False
    threading.Thread(target=_run_bg, args=(batch_size,), daemon=True).start()
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
    print(process_batch())
