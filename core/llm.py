"""Volání Ollamy z jádra (překlad, kategorizace). Používá rychlý model."""
from __future__ import annotations

import json
import logging

import httpx

from . import config

log = logging.getLogger("core.llm")

TOP = [
    "maso", "ryby a mořské plody", "mléčné výrobky", "vejce", "zelenina",
    "ovoce", "obiloviny a pečivo", "luštěniny", "ořechy a semínka",
    "tuky a oleje", "koření a bylinky", "sladidla", "nápoje", "ostatní",
]


def _generate(prompt: str) -> dict | None:
    c = config.get()
    try:
        r = httpx.post(
            f"{c['ollama_url'].rstrip('/')}/api/generate",
            json={
                "model": config.fast_model(),
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "think": False,
                "keep_alive": c["ollama_keep_alive"],
                "options": {"temperature": 0},
            },
            timeout=180,
        )
        r.raise_for_status()
        return json.loads(r.json()["response"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Ollama volání selhalo: %s", exc)
        return None


def translate_fields(title: str, ingredients: list[str], instructions: str) -> dict | None:
    payload = {"title": title or "", "ingredients": list(ingredients), "instructions": instructions or ""}
    prompt = (
        "Přelož tento recept do češtiny. Zachovej přesně počet a pořadí ingrediencí. "
        "Jednotky a množství ponech, jen přelož názvy. Odpověz POUZE JSON "
        '{"title": string, "ingredients": [string], "instructions": string}.\n'
        f"Recept: {json.dumps(payload, ensure_ascii=False)}"
    )
    out = _generate(prompt)
    if not out:
        return None
    ing = out.get("ingredients")
    if not isinstance(ing, list) or len(ing) != len(ingredients):
        return None
    return {
        "title": (out.get("title") or title or "").strip(),
        "ingredients": [str(x) for x in ing],
        "instructions": out.get("instructions") or instructions or "",
    }


def categorize_batch(pairs: list[tuple[int, str]]) -> dict[int, str]:
    """pairs=[(id,name)] → {id: category_path}."""
    listing = "\n".join(f"{i}. {name}" for i, (_id, name) in enumerate(pairs))
    prompt = (
        "Zařaď každou potravinu do hierarchické kategorie. Oddělovač ' > ', max 3 úrovně, "
        f"první úroveň z: {', '.join(TOP)}. Příklad: 'kuřecí prsa' → 'maso > drůbeží > kuřecí'. "
        'Odpověz POUZE JSON {"items":[{"i":<index>,"category_path":"..."}]}.\n'
        f"Potraviny:\n{listing}"
    )
    out = _generate(prompt)
    result: dict[int, str] = {}
    if not out:
        return result
    for it in out.get("items", []):
        try:
            idx = int(it.get("i"))
            path = str(it.get("category_path") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if 0 <= idx < len(pairs) and path and path.split(">")[0].strip().lower() in TOP:
            result[pairs[idx][0]] = path
    return result
