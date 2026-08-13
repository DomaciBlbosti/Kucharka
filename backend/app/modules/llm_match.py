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

from sqlalchemy import select, func, update as sa_update
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
    "nonfood": 0, "errors": 0, "created": 0, "last_error": None,
    "ctx_done": 0, "ctx_total": 0, "ctx_applied": 0, "ctx_removed": 0,
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
            .where(
                RecipeIngredient.ingredient_id.is_(None),
                RecipeIngredient.nonfood.is_(False),
            )
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

    __slots__ = ("key", "sample", "row_ids", "recipe_ids", "context_title")

    def __init__(self, key: str):
        self.key = key
        self.sample: str = ""
        self.row_ids: list[int] = []
        self.recipe_ids: set[int] = set()
        # název jednoho z receptů – levný kontext pro LLM ("bazalka" u
        # "Cannelloni s boloňskou omáčkou" je jasnější než "bazalka" samotná)
        self.context_title: str | None = None

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
            RecipeIngredient.nonfood.is_(False),
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
        r.nonfood = False  # kdyby byl dřív omylem označen jako ne-surovina
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
            # non-food: řádky zůstávají bez suroviny záměrně – označ je,
            # ať se přestanou počítat mezi čekající a příště se ani nenačítají
            _mark_rows_nonfood(db, g.row_ids)
            stats["dict_nonfood"] += len(g.row_ids)
            pending_commit += len(g.row_ids)
            if pending_commit >= 1000:
                db.commit()
                pending_commit = 0
    db.commit()
    return stats, affected


def _mark_rows_nonfood(db: Session, row_ids: list[int], flag: bool = True) -> None:
    for start in range(0, len(row_ids), 500):
        db.execute(
            sa_update(RecipeIngredient)
            .where(RecipeIngredient.id.in_(row_ids[start:start + 500]))
            .values(nonfood=flag)
        )


# ─── Katalog rozhodnutí ──────────────────────────────────────────────────────

def _upsert_decision(
    db: Session,
    key: str,
    sample: str,
    *,
    status: str,
    category: str | None = None,
    ingredient_id: int | None = None,
    suggested_name: str | None = None,
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
    d.suggested_name = (suggested_name or None) and suggested_name[:200]
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
                    "name_cs": {"type": "string"},
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
- name_cs: kanonický český název suroviny (1. pád, jednotné číslo, bez množství). Vyplň VŽDY u category="food" – i když v databázi nic nepasuje (podle name_cs se pak surovina založí). U ostatních kategorií nech prázdné.
- category: "food" pro suroviny (i dochucovadla a přípravky jako ztužovač šlehačky, kypřicí prášek); "equipment" (forma, lžíce, struhadlo); "garnish" (na ozdobu); "packaging" (folie, alobal, pečicí papír); "unknown" jinak.
- Text končící dvojtečkou ("Na těsto:", "Dále:") je nadpis skupiny, ne surovina → category="unknown".
- confidence: 0.9+ = jistá shoda; 0.7-0.9 = pravděpodobná; pod 0.7 = nejistá.
- Cizojazyčné názvy přelož: "chicken breast" → kuřecí prsa; "soy sauce" → sójová omáčka; "cilantro" → koriandr.
- Při nejistotě dej nižší confidence, nehádej.

Příklady chování:
- "chicken breast" → najdi "kuřecí prsa" v databázi, category="food", name_cs="kuřecí prsa", confidence=0.95
- "1 ztužovač šlehačky" (v databázi není) → category="food", ingredient_id=null, name_cs="ztužovač šlehačky", confidence=0.9
- "silikonová forma na muffiny" → category="equipment", ingredient_id=null, confidence=0.9
- "trochu lásky :)" → category="unknown", ingredient_id=null, confidence=0.0

Databáze surovin (id: name):
"""

# Návrh nového názvu suroviny musí vypadat jako název, ne věta/poznámka.
_MAX_NEW_NAME_LEN = 60
_MAX_NEW_NAME_WORDS = 6


def _plausible_new_name(name: str) -> bool:
    n = (name or "").strip()
    return (
        2 <= len(n) <= _MAX_NEW_NAME_LEN
        and len(n.split()) <= _MAX_NEW_NAME_WORDS
        and not n.endswith(":")
        and not any(ch.isdigit() for ch in n)
    )


def _make_prompt(
    catalog: list[tuple[int, str]],
    inputs: list[str],
    contexts: list[str | None] | None = None,
) -> str:
    """`contexts` = název receptu k jednotlivým položkám – levná náhrada za
    posílání celého receptu: pár tokenů navíc a model ví, v jakém jídle se
    surovina objevila ("bazalka" u "Cannelloni s boloňskou omáčkou")."""
    catalog_str = "\n".join(f"{cid}: {name}" for cid, name in catalog)
    lines = []
    for i, t in enumerate(inputs):
        ctx = contexts[i] if contexts and i < len(contexts) else None
        lines.append(f"{i}: {t} — recept: {ctx[:60]}" if ctx else f"{i}: {t}")
    inputs_str = "\n".join(lines)
    return f"{_PROMPT_HEADER}{catalog_str}\n\nSuroviny k přiřazení (i: text — recept: odkud pochází):\n{inputs_str}\n"


# Po tolika selhaných dávkách V ŘADĚ se běh zastaví – když padá úplně všechno
# (Ollama spadlá, model chybí, timeout moc krátký), nemá smysl hodiny mlít
# další selhávající dávky. Položky zůstanou jako 'error' a příští běh je
# zkusí znovu; skutečná příčina je vidět v UI (last_error).
MAX_CONSECUTIVE_BATCH_FAILURES = 5


def _call_llm(prompt: str) -> dict | None:
    return llmclient.structured_json(
        prompt,
        schema=_RESPONSE_SCHEMA,
        timeout=max(30, settings.llm_match_timeout_s),
        temperature=settings.llm_match_temperature,
        num_ctx=settings.llm_match_num_ctx,
        ollama_model=settings.llm_match_model or settings.ollama_fast_model,
    )


# ─── Tvorba nových surovin ───────────────────────────────────────────────────

def get_or_create_ingredient(db: Session, name: str) -> Ingredient:
    """Najdi surovinu podle názvu (case-insensitive), nebo ji založ.

    Nové suroviny mají source='ollama' → v UI se počítají do "odhadované
    výživy" a NutriDatabáze je při importu zpřesní/sloučí."""
    name = name.strip()
    ing = db.scalar(
        select(Ingredient).where(func.lower(Ingredient.name_cs) == name.lower())
    )
    if ing is None:
        ing = Ingredient(name_cs=name, source="ollama")
        db.add(ing)
        db.flush()
    return ing


_NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "kcal_100g": {"type": ["number", "null"]},
                    "protein_100g": {"type": ["number", "null"]},
                    "carbs_100g": {"type": ["number", "null"]},
                    "fat_100g": {"type": ["number", "null"]},
                    "density": {"type": ["number", "null"]},
                    "category": {"type": ["string", "null"]},
                },
                "required": ["i", "kcal_100g"],
            },
        }
    },
    "required": ["items"],
}


def estimate_nutrition(db: Session, ingredients: list[Ingredient], batch: int = 40) -> int:
    """Dávkově odhadne výživu /100 g nově založených surovin (jedno LLM volání
    na `batch` položek). Selhání nevadí – surovina zůstane bez výživy a
    NutriDatabáze/ruční editace ji doplní později. Vrací počet doplněných."""
    todo = [i for i in ingredients if i.kcal_100g is None]
    filled = 0
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        listing = "\n".join(f"{j}: {ing.name_cs}" for j, ing in enumerate(chunk))
        prompt = (
            "Pro každou potravinu níže odhadni typické výživové hodnoty na 100 g "
            "a hustotu (g na 1 ml; null pokud nedává smysl). category = jedno "
            "slovo (např. maso, zelenina, koření, pečivo). Odpověz POUZE JSON "
            '{"items":[{"i":<index>,"kcal_100g":number,"protein_100g":number,'
            '"carbs_100g":number,"fat_100g":number,"density":number|null,'
            '"category":string}]}.\n'
            f"Potraviny:\n{listing}"
        )
        out = llmclient.structured_json(prompt, schema=_NUTRITION_SCHEMA, timeout=120,
                                        num_ctx=8192)
        if out is None:
            log.warning("odhad výživy dávky selhal – %s surovin zůstává bez výživy",
                        len(chunk))
            continue
        by_i: dict[int, dict] = {}
        for it in out.get("items", []):
            try:
                by_i[int(it.get("i"))] = it
            except (TypeError, ValueError):
                continue
        for j, ing in enumerate(chunk):
            it = by_i.get(j)
            if it is None or it.get("kcal_100g") is None:
                continue
            try:
                ing.kcal_100g = float(it["kcal_100g"])
                ing.protein_100g = float(it["protein_100g"]) if it.get("protein_100g") is not None else None
                ing.carbs_100g = float(it["carbs_100g"]) if it.get("carbs_100g") is not None else None
                ing.fat_100g = float(it["fat_100g"]) if it.get("fat_100g") is not None else None
                ing.density = float(it["density"]) if it.get("density") is not None else None
                if not ing.category and it.get("category"):
                    ing.category = str(it["category"])[:120]
                filled += 1
            except (TypeError, ValueError):
                continue
        db.commit()
    return filled


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
    error_detail: str | None = None,
) -> tuple[dict, set[int], list[Ingredient]]:
    """Zapíše rozhodnutí pro KAŽDOU položku dávky.

    Vrátí (statistiky, dotčené recepty, nově založené suroviny k odhadu výživy).
    """
    stats = {"applied": 0, "suggested": 0, "no_match": 0, "nonfood": 0,
             "errors": 0, "created": 0}
    affected: set[int] = set()
    created: list[Ingredient] = []

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
            detail = (
                f"LLM volání selhalo: {error_detail}" if error_detail
                else "model položku v odpovědi vynechal"
            )
            _upsert_decision(
                db, g.key, g.sample, status="error", model=model_name,
                occurrences=occurrences, bump_attempts=True,
                error=detail[:500],
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
            _mark_rows_nonfood(db, g.row_ids)
            _upsert_decision(db, g.key, g.sample, status="nonfood", category=category,
                             confidence=confidence, model=model_name, occurrences=occurrences)
            stats["nonfood"] += 1
            continue

        name_cs = (it.get("name_cs") or "").strip()

        if ing_id is None or ing_id not in valid_ids:
            # Katalog nic nenabídl. Když LLM vrátilo věrohodný název, surovina
            # nejspíš v DB vůbec není → založit (auto_ingredients), nebo
            # aspoň uložit návrh, ať jde založit jedním klikem z katalogu.
            if _plausible_new_name(name_cs) and confidence >= min_conf:
                if settings.auto_ingredients:
                    ing = get_or_create_ingredient(db, name_cs)
                    valid_ids.add(ing.id)
                    if ing.kcal_100g is None:
                        created.append(ing)
                    _upsert_alias(db, g.sample, lookup_key=g.key, ingredient_id=ing.id,
                                  kind="food", source="llm", confidence=confidence)
                    affected |= _apply_rows(db, g.row_ids, ing)
                    _upsert_decision(db, g.key, g.sample, status="applied",
                                     category="food", ingredient_id=ing.id,
                                     suggested_name=name_cs, confidence=confidence,
                                     model=model_name, occurrences=occurrences)
                    stats["applied"] += 1
                    stats["created"] += 1
                else:
                    _upsert_decision(db, g.key, g.sample, status="suggested",
                                     category="food", suggested_name=name_cs,
                                     confidence=confidence, model=model_name,
                                     occurrences=occurrences)
                    stats["suggested"] += 1
            else:
                _upsert_decision(
                    db, g.key, g.sample, status="no_match", category="food",
                    suggested_name=name_cs if _plausible_new_name(name_cs) else None,
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
                "errors": len(batch), "created": 0}, set(), []
    return stats, affected, created


# ─── Jedna dávka s auto-půlením při timeoutu ─────────────────────────────────

def _attempt_batch(
    db: Session,
    chunk: list[_Group],
    catalog: list[tuple[int, str]],
    valid_ids: set[int],
    min_conf: float,
    model_name: str,
    depth: int = 0,
) -> tuple[dict, set[int], list[Ingredient], bool, int]:
    """Zpracuje dávku; při timeoutu ji rozdělí na poloviny a zkusí znovu
    (max 2 úrovně: 40 → 20 → 10). Lokální model, který se s velkým promptem
    nevejde do limitu, tak dávky dokončí sám – bez ručního ladění velikosti.

    Vrátí (stats, affected, created, failed_all, calls).
    """
    prompt = _make_prompt(
        catalog, [g.sample for g in chunk],
        contexts=[g.context_title for g in chunk],
    )
    resp = _call_llm(prompt)
    if resp is not None:
        stats, affected, created = _process_response(
            db, resp, chunk, valid_ids, min_conf, model_name
        )
        return stats, affected, created, False, 1

    err = llmclient.last_error() or ""
    timeoutish = "timed out" in err.lower() or "timeout" in err.lower()
    if timeoutish and depth < 2 and len(chunk) >= 4:
        mid = len(chunk) // 2
        log.warning(
            "dávka %s položek vypršela (%s) – zkouším po polovinách (%s + %s)",
            len(chunk), err[:80], mid, len(chunk) - mid,
        )
        s1, a1, c1, f1, n1 = _attempt_batch(
            db, chunk[:mid], catalog, valid_ids, min_conf, model_name, depth + 1)
        s2, a2, c2, f2, n2 = _attempt_batch(
            db, chunk[mid:], catalog, valid_ids, min_conf, model_name, depth + 1)
        merged = {k: s1.get(k, 0) + s2.get(k, 0) for k in
                  ("applied", "suggested", "no_match", "nonfood", "errors", "created")}
        return merged, a1 | a2, c1 + c2, f1 and f2, 1 + n1 + n2

    stats, affected, created = _process_response(
        db, None, chunk, valid_ids, min_conf, model_name, error_detail=err
    )
    return stats, affected, created, True, 1


# ─── Fáze 3: kontextové dořešení po receptech ────────────────────────────────
# Dávková fáze pracuje s deduplikovanými texty BEZ kontextu – to stačí na
# běžné suroviny, ale ne na útržky postupu, poznámky a fragmenty ze scrapu
# ("dle chuti dosolíme", "recept pochází z…"). Ty skončí jako 'no_match'.
# Kontextová fáze je vezme PO RECEPTECH: LLM dostane celý recept (název,
# všechny suroviny s už napárovanými názvy, zkrácený postup) a rozhodne,
# jestli je nerozpoznaný řádek surovina (→ založit/napárovat), poznámka či
# kus textu (→ smazat, jako nadpisy), nebo pomůcka/obal.

CONTEXT_MAX_RECIPES_PER_RUN = 300   # strop na jeden běh; zbytek příště
_CTX_MIN_CONF_ACTION = 0.7          # pod tímhle prahem se nic nemaže/nezakládá
_CTX_MAX_CONSECUTIVE_ERRORS = 3

_CTX_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "verdict": {
                        "type": "string",
                        "enum": ["ingredient", "compound", "note", "nonfood", "unknown"],
                    },
                    "name_cs": {"type": "string"},
                    "names_cs": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["i", "verdict", "confidence"],
            },
        }
    },
    "required": ["items"],
}

_CTX_PROMPT_HEADER = """Jsi expert na české recepty. Dostaneš JEDEN recept a několik řádků z jeho seznamu surovin, které se nepodařilo rozpoznat. Podle kontextu celého receptu urči pro KAŽDÝ očíslovaný řádek:
- verdict "ingredient": řádek JE jedna surovina → vyplň name_cs (kanonický český název, 1. pád jednotného čísla, bez množství)
- verdict "compound": řádek obsahuje VÍCE surovin najednou (např. "sůl a čerstvě namletý pepř") → vyplň names_cs = seznam kanonických názvů (["sůl", "pepř"])
- verdict "note": NENÍ surovina – poznámka, útržek postupu nebo textu stránky, reklama, odkaz (např. "dle chuti dosolíme", "recept pochází z webu…")
- verdict "nonfood": kuchyňská pomůcka nebo obal (forma, alobal, pečicí papír)
- verdict "unknown": nelze určit
confidence: 0–1. Odpověz POUZE JSON {"items":[{"i":<index>,"verdict":"...","name_cs":"...","names_cs":[],"confidence":0.9}]} s právě jedním objektem pro každý index.
"""


def _split_compound_rows(
    db: Session,
    key: str,
    row_ids: list[int],
    names: list[str],
    conf: float,
    created_sink: list[Ingredient],
) -> set[int]:
    """Rozdělí složené řádky ("sůl a pepř") na samostatné suroviny: každý
    původní řádek nahradí N novými řádky napárovanými na jednotlivé suroviny
    (množství neznáme – zůstává prázdné) a původní smaže. Vrátí dotčené recepty."""
    affected: set[int] = set()
    uniq: list[Ingredient] = []
    seen_ids: set[int] = set()
    for name in names:
        ing = get_or_create_ingredient(db, name)
        if ing.id in seen_ids:
            continue
        seen_ids.add(ing.id)
        uniq.append(ing)
        if ing.kcal_100g is None and ing not in created_sink:
            created_sink.append(ing)

    for row_id in row_ids:
        row = db.get(RecipeIngredient, row_id)
        if row is None or row.ingredient_id is not None:
            continue
        for ing in uniq:
            db.add(RecipeIngredient(
                recipe_id=row.recipe_id,
                raw_text=ing.name_cs,
                ingredient_id=ing.id,
            ))
        affected.add(row.recipe_id)
        db.delete(row)
    return affected


def _context_pass(
    db: Session,
    min_conf: float,
    model_name: str,
    created_sink: list[Ingredient],
) -> tuple[dict, set[int]]:
    """Vrátí (statistiky, dotčené recepty). Necommitované změny commitne po receptu."""
    stats = {"ctx_recipes": 0, "ctx_applied": 0, "ctx_suggested": 0,
             "ctx_removed": 0, "ctx_nonfood": 0, "ctx_unknown": 0, "ctx_errors": 0}
    affected: set[int] = set()

    # kandidáti: 'no_match' (dávková fáze neuměla rozhodnout) i 'error'
    # (včetně těch na stropu pokusů – kontext je jejich druhá šance),
    # každý projde kontextem nejvýš jednou (ctx_tried)
    decisions_map = {
        d.lookup_key: d for d in db.scalars(
            select(MatchDecision).where(
                MatchDecision.status.in_(("no_match", "error")),
                MatchDecision.ctx_tried.is_(False),
            )
        ).all()
    }
    if not decisions_map:
        return stats, affected

    # živé nenapárované řádky těchhle klíčů, po receptech
    rows = db.execute(
        select(RecipeIngredient.id, RecipeIngredient.raw_text, RecipeIngredient.recipe_id)
        .where(
            RecipeIngredient.ingredient_id.is_(None),
            RecipeIngredient.nonfood.is_(False),
            RecipeIngredient.raw_text.is_not(None),
        )
    ).all()
    per_recipe: dict[int, dict[str, tuple[str, list[int]]]] = {}
    global_rows: dict[str, list[int]] = defaultdict(list)
    for row_id, raw_text, recipe_id in rows:
        raw = (raw_text or "").strip()
        key = make_lookup_key(raw) if raw else ""
        if not key or key not in decisions_map:
            continue
        bucket = per_recipe.setdefault(recipe_id, {})
        sample, ids = bucket.get(key, (raw, []))
        ids.append(row_id)
        bucket[key] = (sample, ids)
        global_rows[key].append(row_id)

    # recepty s nejvíc nerozpoznanými řádky první; strop na běh
    ordered = sorted(per_recipe.items(), key=lambda kv: -len(kv[1]))
    ordered = ordered[:CONTEXT_MAX_RECIPES_PER_RUN]
    with _lock:
        _state.update(ctx_total=len(ordered), ctx_done=0)

    processed_keys: set[str] = set()
    consecutive_errors = 0
    for done_i, (rid, buckets) in enumerate(ordered, 1):
        unresolved = [(k, v) for k, v in buckets.items() if k not in processed_keys]
        if not unresolved:
            with _lock:
                _state.update(ctx_done=done_i)
            continue
        recipe = db.get(Recipe, rid)
        if recipe is None:
            continue

        lines = []
        for ri in recipe.ingredients:
            mark = f" → {ri.ingredient.name_cs}" if ri.ingredient else "  (nerozpoznáno)"
            lines.append(f"- {ri.raw_text}{mark}")
        instructions = (recipe.instructions or "").strip()[:400]
        numbered = "\n".join(f"{i}: {sample}" for i, (_k, (sample, _ids)) in enumerate(unresolved))
        prompt = (
            f"{_CTX_PROMPT_HEADER}\n"
            f"Recept: {recipe.title}\n"
            + (f"Postup (zkráceno): {instructions}\n" if instructions else "")
            + "Seznam surovin receptu:\n" + "\n".join(lines)
            + f"\n\nNerozpoznané řádky k posouzení (i: text):\n{numbered}\n"
        )
        resp = llmclient.structured_json(
            prompt, schema=_CTX_SCHEMA,
            timeout=max(30, settings.llm_match_timeout_s),
            num_ctx=settings.llm_match_num_ctx,
            ollama_model=settings.llm_match_model or settings.ollama_fast_model,
        )
        stats["ctx_recipes"] += 1
        if resp is None:
            stats["ctx_errors"] += 1
            consecutive_errors += 1
            with _lock:
                _state.update(ctx_done=done_i, last_error=llmclient.last_error())
            if consecutive_errors >= _CTX_MAX_CONSECUTIVE_ERRORS:
                log.warning("kontextová fáze: %s chyb v řadě – zbytek příště", consecutive_errors)
                break
            continue
        consecutive_errors = 0

        by_i: dict[int, dict] = {}
        for it in resp.get("items", []):
            try:
                by_i[int(it.get("i"))] = it
            except (TypeError, ValueError):
                continue

        for idx, (key, (sample, row_ids)) in enumerate(unresolved):
            d = decisions_map.get(key)
            it = by_i.get(idx)
            if d is None or it is None:
                continue  # vynechané se zkusí příště
            verdict = (it.get("verdict") or "unknown").lower()
            try:
                conf = float(it.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            name = (it.get("name_cs") or "").strip()
            d.ctx_tried = True  # kontextem prošlo – neopakovat
            d.updated_at = datetime.utcnow()

            if verdict == "ingredient" and conf >= min_conf and _plausible_new_name(name):
                if settings.auto_ingredients:
                    ing = get_or_create_ingredient(db, name)
                    if ing.kcal_100g is None:
                        created_sink.append(ing)
                    _upsert_alias(db, sample, lookup_key=key, ingredient_id=ing.id,
                                  kind="food", source="llm", confidence=conf)
                    affected |= _apply_rows(db, global_rows[key], ing)
                    d.status = "applied"
                    d.category = "food"
                    d.ingredient_id = ing.id
                    d.suggested_name = name[:200]
                    d.confidence = conf
                    d.model = model_name
                    stats["ctx_applied"] += 1
                else:
                    d.status = "suggested"
                    d.category = "food"
                    d.suggested_name = name[:200]
                    d.confidence = conf
                    d.model = model_name
                    stats["ctx_suggested"] += 1
                processed_keys.add(key)
            elif verdict == "compound" and conf >= _CTX_MIN_CONF_ACTION:
                names = [n.strip() for n in (it.get("names_cs") or [])
                         if _plausible_new_name((n or "").strip())]
                if len(names) >= 2:
                    # rozděl VŠECHNY řádky s tímhle klíčem (napříč recepty)
                    affected |= _split_compound_rows(
                        db, key, global_rows[key], names, conf, created_sink)
                    global_rows[key] = []
                    d.status = "applied"
                    d.category = "food"
                    d.suggested_name = (" + ".join(names))[:200]
                    d.confidence = conf
                    d.model = model_name
                    d.error = f"složený řádek rozdělen na: {', '.join(names)}"
                    stats["ctx_applied"] += 1
                    processed_keys.add(key)
                else:
                    stats["ctx_unknown"] += 1
            elif verdict == "nonfood" and conf >= _CTX_MIN_CONF_ACTION:
                _upsert_alias(db, sample, lookup_key=key, ingredient_id=None,
                              kind="equipment", source="llm", confidence=conf)
                _mark_rows_nonfood(db, global_rows[key])
                d.status = "nonfood"
                d.category = "equipment"
                d.confidence = conf
                d.model = model_name
                stats["ctx_nonfood"] += 1
                processed_keys.add(key)
            elif verdict == "note" and conf >= _CTX_MIN_CONF_ACTION:
                # poznámka/kus textu → smazat řádky TOHOHLE receptu
                for row_id in row_ids:
                    obj = db.get(RecipeIngredient, row_id)
                    if obj is not None:
                        db.delete(obj)
                stats["ctx_removed"] += len(row_ids)
                remaining = [r for r in global_rows[key] if r not in set(row_ids)]
                global_rows[key] = remaining
                if not remaining:
                    d.status = "ignored"
                    d.error = "poznámka/kus textu, ne surovina (kontextová kontrola)"
                    d.model = model_name
                    processed_keys.add(key)
            else:
                stats["ctx_unknown"] += 1

        db.commit()
        with _lock:
            _state.update(
                ctx_done=done_i,
                ctx_applied=stats["ctx_applied"], ctx_removed=stats["ctx_removed"],
            )
    return stats, affected


# ─── Přepočet dotčených receptů ──────────────────────────────────────────────

def _fill_rows_kcal(db: Session, created: list[Ingredient]) -> None:
    """Řádky napárované na nové suroviny dostaly kcal=None (výživa se
    odhaduje až po napárování) – dopočítej je teď."""
    for ing in created:
        if ing.kcal_100g is None:
            continue
        rows = db.scalars(
            select(RecipeIngredient).where(
                RecipeIngredient.ingredient_id == ing.id,
                RecipeIngredient.kcal.is_(None),
            )
        ).all()
        for r in rows:
            r.grams = grams_for(r.amount, r.unit, ing)
            r.kcal = kcal_for(r.grams, ing)
    db.commit()


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
            nonfood=0, errors=0, created=0, last_error=None,
            ctx_done=0, ctx_total=0, ctx_applied=0, ctx_removed=0,
            finished_at=None,
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
        return _run(batch_size)
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

        # ─── Úklid mrtvých rozhodnutí ───────────────────────────────────
        # Nevyřízená rozhodnutí (chyba/bez shody/návrh), jejichž řádky už
        # neexistují – mezitím je smazal purge (nadpisy, poznámky) nebo je
        # napároval fuzzy/slovník, který rozhodnutí neaktualizuje. V katalogu
        # z nich zbývá jen matoucí nepořádek (v produkci ~2000 "chyb" bez
        # jediného živého řádku). Finální stavy (applied/nonfood/ignored)
        # zůstávají jako audit.
        live_keys = set(groups.keys())
        stale = [
            d for d in db.scalars(
                select(MatchDecision).where(
                    MatchDecision.status.in_(("no_match", "error", "suggested"))
                )
            ).all()
            if d.lookup_key not in live_keys
        ]
        for d in stale:
            db.delete(d)
        if stale:
            db.commit()
            totals["stale_cleaned"] = len(stale)
            log.info("úklid katalogu: smazáno %s rozhodnutí bez živých řádků", len(stale))

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
            # Dávková fáze nemá co dělat, ale kontextové dořešení 'no_match'
            # položek po receptech může stále běžet (typický stav po prvním
            # projetí celé fronty).
            with _lock:
                _state.update(phase="context")
            created_ingredients: list[Ingredient] = []
            ctx_stats, ctx_affected = _context_pass(
                db, min_conf, model_name, created_ingredients
            )
            totals.update(ctx_stats)
            affected |= ctx_affected
            if created_ingredients:
                with _lock:
                    _state.update(phase="nutrition")
                totals["nutrition_filled"] = estimate_nutrition(db, created_ingredients)
                _fill_rows_kcal(db, created_ingredients)
            with _lock:
                _state.update(phase="kcal")
            totals["reenriched"] = _finalize_recipes(db, affected)
            log.info("LLM match: dávková fronta prázdná (%s už rozhodnuto). %s", skipped_decided, totals)
            return totals

        # Nejčastější texty první – položky s stovkami výskytů se vyřeší
        # v prvních dávkách, i kdyby se běh přerušil.
        queue.sort(key=lambda g: len(g.row_ids), reverse=True)

        # Kontext pro prompt: název jednoho receptu ke každé skupině.
        title_ids = {min(g.recipe_ids) for g in queue if g.recipe_ids}
        titles: dict[int, str] = {}
        ids_list = sorted(title_ids)
        for start in range(0, len(ids_list), 1000):
            for rid, title in db.execute(
                select(Recipe.id, Recipe.title).where(Recipe.id.in_(ids_list[start:start + 1000]))
            ).all():
                titles[rid] = title
        for g in queue:
            if g.recipe_ids:
                g.context_title = titles.get(min(g.recipe_ids))

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
                     "errors": 0, "created": 0, "batches": 0}
        created_ingredients: list[Ingredient] = []
        done_items = 0
        consecutive_failures = 0
        for idx, chunk in enumerate(chunks):
            catalog = catalogs[idx] or static_catalog
            stats, batch_affected, batch_created, failed_all, calls = _attempt_batch(
                db, chunk, catalog, valid_ids, min_conf, model_name
            )
            err_detail = llmclient.last_error() if failed_all else None
            run_stats["batches"] += calls
            for k in ("applied", "suggested", "no_match", "nonfood", "errors", "created"):
                run_stats[k] += stats[k]
            affected |= batch_affected
            created_ingredients.extend(batch_created)
            done_items += len(chunk)
            with _lock:
                _state.update(
                    done=done_items,
                    applied=run_stats["applied"], suggested=run_stats["suggested"],
                    no_match=run_stats["no_match"], nonfood=run_stats["nonfood"],
                    errors=run_stats["errors"], created=run_stats["created"],
                    last_error=err_detail,
                )
            if failed_all:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                    # padá úplně všechno – zastavit, ať se hodiny nemele naprázdno
                    log.error(
                        "LLM match: %s dávek selhalo v řadě (%s) – běh se zastavuje, "
                        "zbytek fronty zůstává na příště.",
                        consecutive_failures, err_detail,
                    )
                    totals["aborted"] = (
                        f"zastaveno po {consecutive_failures} selhaných dávkách v řadě: "
                        f"{err_detail or 'neznámá chyba'}"
                    )
                    break
                log.warning(
                    "dávka %s selhala (%s), čekám %ss na zotavení",
                    run_stats["batches"], err_detail, settings.llm_match_failure_pause_s,
                )
                time.sleep(settings.llm_match_failure_pause_s)
            else:
                consecutive_failures = 0
                if settings.llm_match_batch_pause_s and not settings.llm_api_enabled:
                    # oddych jen pro lokální GPU; komerční API pauzy nepotřebuje
                    time.sleep(settings.llm_match_batch_pause_s)

        # ─── Fáze 3: kontextové dořešení 'no_match' položek po receptech ─
        # Jen když LLM reálně odpovídá (ne po circuit breakeru).
        if "aborted" not in totals:
            with _lock:
                _state.update(phase="context")
            ctx_stats, ctx_affected = _context_pass(
                db, min_conf, model_name, created_ingredients
            )
            totals.update(ctx_stats)
            affected |= ctx_affected

        # ─── Odhad výživy nově založených surovin (dávkově) ─────────────
        if created_ingredients:
            with _lock:
                _state.update(phase="nutrition")
            totals["nutrition_filled"] = estimate_nutrition(db, created_ingredients)
            _fill_rows_kcal(db, created_ingredients)

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
