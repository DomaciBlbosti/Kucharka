"""Skenování účtenky z obchodu ve více úsecích (dlouhá účtenka „panoramaticky").

Přístup záměrně NENÍ pixelové sešívání fotek (to je na text křehké kvůli
rozostření/perspektivě). Místo toho:
  1. Každý vyfocený úsek pošleme samostatně vision modelu v Ollamě, který
     vrátí jen názvy nakoupených položek (JSON).
  2. Překryv mezi sousedními úseky (uživatel fotí s malým přesahem) slučujeme
     na úrovni TEXTU – fuzzy porovnáním konce jednoho seznamu se začátkem
     druhého, ne porovnáním pixelů.
  3. Výsledné položky napárujeme na kanonické suroviny stejnou pipeline jako
     recepty (normalizer.match_ingredient) a vrátíme uživateli k potvrzení.
"""
from __future__ import annotations

import base64
import io
import json
import logging

import httpx
from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from ..config import settings
from .normalizer import match_ingredient
from .textmerge import merge_lists

log = logging.getLogger("kucharka.receipt")

_MAX_WIDTH = 1400


def preprocess_image(raw: bytes) -> bytes:
    """Zmenši a nasměruj podle EXIF, ať je přenos rychlý a model má rozumný vstup."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > _MAX_WIDTH:
        ratio = _MAX_WIDTH / img.width
        img = img.resize((_MAX_WIDTH, int(img.height * ratio)))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


_PROMPT = (
    "Toto je fotografie ÚSEKU účtenky z obchodu (může jít jen o část delší "
    "účtenky, fotografované po kouscích odshora dolů). Vypiš POUZE názvy "
    "nakoupených produktů/potravin, v pořadí jak jsou na účtence. "
    "IGNORUJ: ceny, množství, slevy, mezisoučty, DIČ/IČO, adresu prodejny, "
    "platební údaje, věrnostní program, čárové kódy, rekapitulaci DPH a "
    "jakýkoliv text, který není název produktu. Pokud úsek žádné produkty "
    "neobsahuje, vrať prázdný seznam. Odpověz POUZE JSON "
    '{"items": ["název1", "název2", ...]}.'
)


def extract_items_from_image(image_bytes: bytes) -> list[str]:
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
        log.warning("OCR odpověď se nepodařilo naparsovat: %s", exc)
        return []
    items = out.get("items", [])
    return [str(x).strip() for x in items if str(x).strip()]


def merge_segments(segments: list[list[str]]) -> list[str]:
    return merge_lists(segments)


def scan_segments(images: list[bytes]) -> dict:
    """Zpracuj všechny úseky a vrať sloučený seznam položek (bez DB)."""
    per_segment = [extract_items_from_image(img) for img in images]
    items = merge_segments(per_segment)
    return {"items": items, "segments": [len(s) for s in per_segment]}


def match_items(db: Session, names: list[str]) -> list[dict]:
    """Napáruj každou položku na kanonickou surovinu (bez commitu – jen náhled)."""
    out = []
    for name in names:
        ing = match_ingredient(db, name)
        out.append({
            "raw_name": name,
            "ingredient_id": ing.id if ing else None,
            "ingredient_name": ing.name_cs if ing else None,
        })
    return out
