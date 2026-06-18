"""Normalizace ingrediencí.

Vstup: volný řádek "2 lžíce hladké mouky"
Výstup: (amount=2, unit="lžíce", ingredient=<Ingredient mouka hladká>)

Postup:
  1) parsování řádku na (množství, jednotka, název) – Ollama, nebo regex fallback
  2) napárování názvu na kanonickou surovinu – nejdřív alias cache, pak fuzzy match
  3) uložení do alias cache pro příště (deterministické a rychlé)
"""
from __future__ import annotations

import json
import re
import unicodedata

import httpx
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Ingredient, IngredientAlias
from .nutrition import PIECE_GRAMS, UNIT_TO_G, UNIT_TO_ML

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
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_fast_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "think": False,  # vypni reasoning (qwen3 ap.) → rychlejší čistý JSON
                "keep_alive": settings.ollama_keep_alive,
                "options": {"temperature": 0},
            },
            timeout=max(settings.http_timeout, 60),
        )
        r.raise_for_status()
        items = json.loads(r.json()["response"]).get("items", [])
        if len(items) != len(lines):
            return None
        out = []
        for it in items:
            amt = it.get("amount")
            out.append(
                (
                    float(amt) if amt is not None else None,
                    (it.get("unit") or None),
                    (it.get("name") or "").strip(),
                )
            )
        return out
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
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_fast_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "think": False,
                "keep_alive": settings.ollama_keep_alive,
                "options": {"temperature": 0},
            },
            timeout=max(settings.http_timeout, 60),
        )
        r.raise_for_status()
        data = json.loads(r.json()["response"])
    except Exception:
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


def normalize_lines(db: Session, lines: list[str]) -> list[dict]:
    """Normalizuj všechny řádky receptu. Parsování dávkově (Ollama), pak párování.

    Řádky, které Ollama nezvládne (nebo je vypnutá), se doparsují regexem.
    """
    parsed = parse_lines_ollama(lines)
    results = []
    for i, text in enumerate(lines):
        if parsed is not None:
            amount, unit, name = parsed[i]
            if not name:
                amount, unit, name = parse_line_regex(text)
        else:
            amount, unit, name = parse_line_regex(text)
        ing = match_ingredient(db, name)
        if ing is None and settings.auto_ingredients:
            ing = create_ingredient_via_llm(db, name)
        results.append(
            {
                "raw_text": text,
                "amount": amount,
                "unit": unit,
                "name": name,
                "ingredient": ing,
            }
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
