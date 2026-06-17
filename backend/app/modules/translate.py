"""Překlad zahraničních receptů do češtiny přes Ollamu.

České recepty (doména .cz nebo text s českou diakritikou) se nepřekládají.
U cizích se přeloží titul, ingredience a postup jedním dotazem; pokud se
nezachová počet ingrediencí, ponecháme originál (kvůli párování surovin).
"""
from __future__ import annotations

import json
import logging

import httpx

from ..config import settings

log = logging.getLogger("kucharka.translate")

_CZ_CHARS = set("ěščřžůňďť")


def looks_czech(domain: str | None, text: str) -> bool:
    if domain and domain.endswith(".cz"):
        return True
    sample = (text or "")[:2000].lower()
    return any(c in _CZ_CHARS for c in sample)


def translate_recipe(data: dict) -> dict:
    """Přelož recept do češtiny, je-li cizí. Vrací (případně) upravený dict."""
    if not settings.translate_to_cs or not settings.ollama_enabled:
        return data

    probe = f"{data.get('title', '')} {data.get('instructions') or ''}"
    if looks_czech(data.get("source_domain"), probe):
        return data

    ingredients = data.get("ingredients", [])
    payload = {
        "title": data.get("title", ""),
        "ingredients": ingredients,
        "instructions": data.get("instructions") or "",
    }
    prompt = (
        "Přelož tento recept do češtiny. Zachovej přesně počet a pořadí "
        "ingrediencí. Jednotky a množství ponech, jen přelož názvy. Odpověz "
        "POUZE JSON objektem "
        '{"title": string, "ingredients": [string], "instructions": string}.\n'
        f"Recept: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "think": False,
                "options": {"temperature": 0},
            },
            timeout=max(settings.http_timeout, 120),
        )
        r.raise_for_status()
        out = json.loads(r.json()["response"])
    except Exception as exc:  # noqa: BLE001
        log.warning("překlad selhal: %s", exc)
        return data

    new_ing = out.get("ingredients")
    if not isinstance(new_ing, list) or len(new_ing) != len(ingredients):
        log.info("překlad zahozen (nesedí počet ingrediencí)")
        return data

    data["title"] = (out.get("title") or data.get("title")).strip()
    data["ingredients"] = [str(x) for x in new_ing]
    if out.get("instructions"):
        data["instructions"] = out["instructions"]
    data["translated_from"] = data.get("source_domain")
    return data
