"""Normalizace ingrediencí.

Vstup: volný řádek "2 lžíce hladké mouky"
Výstup: (amount=2, unit="lžíce", ingredient=<Ingredient mouka hladká>)

Postup:
  1) parsování řádku na (množství, jednotka, název) – Ollama, nebo regex fallback
  2) napárování názvu na kanonickou surovinu – nejdřív alias cache, pak fuzzy match
  3) uložení do alias cache pro příště (deterministické a rychlé)
"""
from __future__ import annotations

import re
import unicodedata

import httpx
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Ingredient, IngredientAlias
from .nutrition import PIECE_GRAMS, UNIT_TO_G, UNIT_TO_ML
from .ollamachat import chat_json

_KNOWN_UNITS = set(UNIT_TO_G) | set(UNIT_TO_ML) | set(PIECE_GRAMS)

# Slova, která chceme z názvu vyhodit, aby se líp párovalo
_STOP = {
    "čerstvý", "čerstvá", "čerstvé", "mletý", "mletá", "mleté", "na",
    "podle", "chuti", "dle", "ks", "kus", "trochu", "trocha", "asi",
}

_NUM_RE = re.compile(r"(\d+[.,]?\d*)")
_FRACTION = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 0.333, "⅔": 0.667}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _norm(s: str) -> str:
    return _strip_accents(s).lower().strip()


def parse_line_regex(text: str) -> tuple[float | None, str | None, str]:
    """Jednoduchý heuristický parser bez LLM."""
    t = text.strip()
    amount: float | None = None
    unit: str | None = None

    # zlomky jako ½
    for sym, val in _FRACTION.items():
        if sym in t:
            amount = val
            t = t.replace(sym, "").strip()
            break

    if amount is None:
        m = _NUM_RE.search(t)
        if m:
            amount = float(m.group(1).replace(",", "."))
            t = (t[: m.start()] + t[m.end():]).strip()

    tokens = t.split()
    if tokens:
        first = _norm(tokens[0])
        if first in _KNOWN_UNITS:
            unit = tokens[0]
            tokens = tokens[1:]

    name = " ".join(tokens).strip(" ,.-")
    return amount, unit, name


def parse_line_ollama(text: str) -> tuple[float | None, str | None, str] | None:
    """Parsování jednoho řádku přes Ollamu. None při jakémkoli problému."""
    res = parse_lines_ollama([text])
    return res[0] if res else None


def parse_lines_ollama(
    lines: list[str],
) -> list[tuple[float | None, str | None, str]] | None:
    """Dávkové parsování všech řádků jedním dotazem (rychlejší než N volání).

    Vrací None, když Ollama není dostupná / odpověď nesedí → volající spadne
    na regex fallback.
    """
    if not settings.ollama_enabled or not lines:
        return None
    numbered = "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines))
    prompt = (
        "Jsi parser ingrediencí z českých receptů. Pro každý řádek urči "
        "množství (číslo nebo null), jednotku (např. g, ml, lžíce, ks; nebo "
        "null) a název suroviny v 1. pádě jednotného čísla. Zachovej pořadí a "
        "počet řádků. Odpověz POUZE JSON objektem ve tvaru "
        '{"items":[{"amount":number|null,"unit":string|null,"name":string}]}.\n\n'
        f"Řádky:\n{numbered}"
    )
    out = chat_json(
        settings.ollama_url,
        settings.ollama_fast_model,
        prompt,
        keep_alive=settings.ollama_keep_alive,
        timeout=max(settings.http_timeout, 60),
    )
    if out is None:
        return None
    try:
        items = out.get("items", [])
        if len(items) != len(lines):
            return None
        parsed = []
        for it in items:
            amt = it.get("amount")
            parsed.append(
                (
                    float(amt) if amt is not None else None,
                    (it.get("unit") or None),
                    (it.get("name") or "").strip(),
                )
            )
        return parsed
    except Exception:
        return None


def _clean_name(name: str) -> str:
    words = [w for w in _norm(name).split() if w not in _STOP]
    return " ".join(words) or _norm(name)


def match_ingredient(db: Session, name: str) -> Ingredient | None:
    """Najdi kanonickou surovinu pro daný název."""
    key = _clean_name(name)
    if not key:
        return None

    # 1) alias cache
    alias = db.scalar(select(IngredientAlias).where(IngredientAlias.alias == key))
    if alias:
        return db.get(Ingredient, alias.ingredient_id)

    # 2) fuzzy match proti názvům surovin
    rows = db.scalars(select(Ingredient)).all()
    if not rows:
        return None
    choices = {ing.id: _norm(ing.name_cs) for ing in rows}
    best = process.extractOne(
        key, choices, scorer=fuzz.token_set_ratio, score_cutoff=82
    )
    if not best:
        return None
    ing_id = best[2]
    ing = db.get(Ingredient, ing_id)

    # 3) zapiš do cache (a commit nechej na volajícím)
    db.add(IngredientAlias(alias=key, ingredient_id=ing_id))
    return ing


def ollama_check() -> dict:
    """Diagnostika Ollamy: dostupnost, seznam modelů, přítomnost nastaveného."""
    if not settings.ollama_enabled:
        return {"enabled": False, "reachable": False, "models": [], "model_ok": False}
    try:
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=8)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
        model_ok = any(
            settings.ollama_model in m or m.startswith(settings.ollama_model)
            for m in models
        )
        return {
            "enabled": True,
            "reachable": True,
            "url": settings.ollama_url,
            "model": settings.ollama_model,
            "model_ok": model_ok,
            "models": models,
        }
    except Exception as exc:
        return {
            "enabled": True,
            "reachable": False,
            "url": settings.ollama_url,
            "model": settings.ollama_model,
            "error": str(exc),
            "models": [],
            "model_ok": False,
        }


def create_ingredient_via_llm(db: Session, name: str) -> Ingredient | None:
    """Vytvoř kanonickou surovinu pomocí Ollamy (odhad výživy /100 g).

    Použije se při dorůstání DB, když se název nenapáruje na existující surovinu.
    Zdroj se označí 'ollama', takže pozdější import z NutriDatabaze data zpřesní.
    """
    if not settings.ollama_enabled:
        return None
    clean = _clean_name(name)
    if not clean:
        return None
    prompt = (
        f"Pro potravinu/surovinu '{name}' vrať typické výživové hodnoty na 100 g. "
        "Odpověz POUZE JSON objektem "
        '{"name_cs": string (1. pád j.č.), "category": string, '
        '"kcal_100g": number, "protein_100g": number, "carbs_100g": number, '
        '"fat_100g": number, "density": number|null (g na 1 ml, jinak null)}. '
        "Pokud to není jedlá surovina, vrať name_cs prázdné."
    )
    data = chat_json(
        settings.ollama_url,
        settings.ollama_fast_model,
        prompt,
        keep_alive=settings.ollama_keep_alive,
        timeout=max(settings.http_timeout, 60),
    )
    if data is None:
        return None

    name_cs = (data.get("name_cs") or "").strip()
    if not name_cs or data.get("kcal_100g") is None:
        return None

    ing = Ingredient(
        name_cs=name_cs,
        category=(data.get("category") or None),
        kcal_100g=_num(data.get("kcal_100g")),
        protein_100g=_num(data.get("protein_100g")),
        carbs_100g=_num(data.get("carbs_100g")),
        fat_100g=_num(data.get("fat_100g")),
        density=_num(data.get("density")),
        source="ollama",
    )
    db.add(ing)
    db.flush()  # potřebujeme id pro alias
    db.add(IngredientAlias(alias=clean, ingredient_id=ing.id))
    return ing


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def normalize_line(db: Session, text: str) -> dict:
    """Plná normalizace jednoho řádku ingredience."""
    return normalize_lines(db, [text])[0]


def normalize_lines(
    db: Session, lines: list[str], *, allow_llm_create: bool | None = None
) -> list[dict]:
    """Normalizuj všechny řádky receptu.

    Výkonově kritická cesta (crawler zpracovává statisíce receptů), proto:

    1. **Parsování regexem první.** `parse_line_regex` zvládne naprostou většinu
       českých receptových řádků ("2 lžíce mouky" → 2/lžíce/mouka) okamžitě.
       LLM (`parse_lines_ollama`) se zavolá JEN na řádky, kde regex nenajde
       název – ne paušálně na celý recept. Měření ukázalo, že paušální LLM
       parsování žralo 5–56 s na recept, prakticky nadarmo.

    2. **Žádná per-surovina LLM tvorba v hot path.** Dřív se pro každou
       nenapárovanou surovinu volal `create_ingredient_via_llm` (5–44 s, často
       marně – model vrátil prázdno). Při crawlu se surovina teď nechá
       nenapárovaná (`ingredient_id=None`); její dotvoření obstará backfill
       (`match` job) na pozadí, dávkově a mimo kritickou cestu. Pro ruční
       přidání jednoho receptu jde chování zapnout přes `allow_llm_create=True`.

    `allow_llm_create=None` → řídí se `settings.auto_ingredients`, ale POUZE
    mimo crawler (crawler si předává False explicitně).
    """
    import logging
    import time

    tlog = logging.getLogger("kucharka.ingest.timing")

    # 1) regex parse pro všechny řádky; posbírej, co regex nezvládl (chybí název)
    t_parse0 = time.perf_counter()
    regex_parsed: list[tuple[float | None, str | None, str]] = [
        parse_line_regex(text) for text in lines
    ]
    need_llm_idx = [i for i, (_a, _u, name) in enumerate(regex_parsed) if not name]

    # LLM jen na zbytek (typicky prázdný seznam → žádné LLM volání)
    if need_llm_idx and settings.ollama_enabled:
        llm_parsed = parse_lines_ollama([lines[i] for i in need_llm_idx])
        if llm_parsed is not None:
            for pos, idx in enumerate(need_llm_idx):
                amt, unit, name = llm_parsed[pos]
                if name:  # doplň jen když LLM opravdu něco našel
                    regex_parsed[idx] = (amt, unit, name)
    parse_s = time.perf_counter() - t_parse0

    if allow_llm_create is None:
        allow_llm_create = settings.auto_ingredients

    match_s = 0.0
    create_s = 0.0
    created = 0
    results = []
    for i, text in enumerate(lines):
        amount, unit, name = regex_parsed[i]

        t_m0 = time.perf_counter()
        ing = match_ingredient(db, name)
        match_s += time.perf_counter() - t_m0

        if ing is None and allow_llm_create:
            t_c0 = time.perf_counter()
            ing = create_ingredient_via_llm(db, name)
            create_s += time.perf_counter() - t_c0
            if ing is not None:
                created += 1

        results.append(
            {
                "raw_text": text,
                "amount": amount,
                "unit": unit,
                "name": name,
                "ingredient": ing,
            }
        )

    tlog.info(
        "  normalize detail: parse=%.2fs match=%.2fs create_llm=%.2fs "
        "(%d nových surovin z %d řádků, LLM parse jen %d řádků)",
        parse_s, match_s, create_s, created, len(lines), len(need_llm_idx),
    )
    return results


if __name__ == "__main__":
    # Rychlý test parseru/Ollamy:  python -m app.modules.normalizer "2 lžíce mouky"
    import sys

    from ..db import SessionLocal
    from ..main import init_db

    init_db()
    db = SessionLocal()
    lines = sys.argv[1:] or ["500 g brambory", "2 lžíce hladké mouky", "špetka soli"]
    print(f"Ollama: {'ON' if settings.ollama_enabled else 'OFF (regex)'}")
    for r in normalize_lines(db, lines):
        ing = r["ingredient"]
        print(
            f"  {r['raw_text']!r:32} -> amount={r['amount']} unit={r['unit']} "
            f"name={r['name']!r} match={ing.name_cs if ing else None}"
        )
    db.commit()
