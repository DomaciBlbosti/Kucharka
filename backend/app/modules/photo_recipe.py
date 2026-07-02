"""Import receptu z fotek papírového/rukou psaného receptu.

Recept může být vyfocený po částech (více úseků/stránek); z každého úseku
vytáhneme přes vision model v Ollamě název, suroviny a postup a slučujeme
je stejnou fuzzy-merge logikou jako u účtenek (viz textmerge.py). Uložení
konceptu pak jede přes existující ingest pipeli (stejnou jako u receptů
stažených z webu) – suroviny se tak normalizují a párují identicky.
"""
from __future__ import annotations

import base64
import json
import logging

import httpx

from ..config import settings
from .receipt import preprocess_image  # sdílené zmenšení/oříznutí podle EXIF
from .textmerge import merge_lists, merge_texts

log = logging.getLogger("kucharka.photo_recipe")

_PROMPT = (
    "Toto je fotografie ÚSEKU papírového nebo rukou psaného receptu (může jít "
    "jen o část delšího receptu, fotografovaného po kouscích odshora dolů). "
    "Vytáhni z něj: název receptu (pokud je na tomto úseku vidět, jinak prázdný "
    "řetězec), seznam surovin PŘESNĚ tak, jak jsou napsané (množství i "
    "jednotka spolu s názvem na jednom řádku), a text postupu přípravy "
    "(pokud je na tomto úseku vidět, jinak prázdný řetězec). Odpověz POUZE "
    'JSON {"title": string, "ingredients": [string], "instructions": string}.'
)


def _extract_segment(image_bytes: bytes) -> dict:
    if not settings.ocr_model:
        raise RuntimeError("OCR model není nastaven (Admin → Nástroje → OCR model).")
    b64 = base64.b64encode(preprocess_image(image_bytes)).decode()
    r = httpx.post(
        f"{settings.ollama_url}/api/generate",
        json={
            "model": settings.ocr_model,
            "prompt": _PROMPT,
            "images": [b64],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        timeout=max(settings.http_timeout, 120),
    )
    r.raise_for_status()
    try:
        out = json.loads(r.json()["response"])
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR receptu se nepodařilo naparsovat: %s", exc)
        return {"title": "", "ingredients": [], "instructions": ""}
    return {
        "title": str(out.get("title") or "").strip(),
        "ingredients": [str(x).strip() for x in out.get("ingredients", []) if str(x).strip()],
        "instructions": str(out.get("instructions") or "").strip(),
    }


def extract_draft(images: list[bytes]) -> dict:
    """Zpracuj všechny úseky a slož je do jednoho konceptu receptu."""
    segments = [_extract_segment(img) for img in images]
    title = next((s["title"] for s in segments if s["title"]), "")
    ingredients = merge_lists([s["ingredients"] for s in segments])
    instructions = merge_texts([s["instructions"] for s in segments])
    return {"title": title, "ingredients": ingredients, "instructions": instructions}
