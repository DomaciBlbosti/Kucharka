"""Import receptu z fotek papírového/rukou psaného receptu.

Recept může být vyfocený po částech (více úseků/stránek); z každého úseku
vytáhneme přes vision model v Ollamě název, suroviny a postup a slučujeme
je stejnou fuzzy-merge logikou jako u účtenek (viz textmerge.py). Uložení
konceptu pak jede přes existující ingest pipeli (stejnou jako u receptů
stažených z webu) – suroviny se tak normalizují a párují identicky.

Suroviny se navíc už v náhledu rozparsují na množství/jednotku/název (bez
LLM, jen rychlý regex) – uživatel je tak může opravit odděleně, ne jako
jeden plochý text.
"""
from __future__ import annotations

import base64
import logging
import re

from ..config import settings
from .normalizer import parse_line_regex
from .ollamachat import chat_json
from .receipt import preprocess_image  # sdílené zmenšení/oříznutí podle EXIF
from .textmerge import merge_lists, merge_texts
from .uploads import save_recipe_photo

log = logging.getLogger("kucharka.photo_recipe")

# Když model na fotce nenajde název (typicky úsek uprostřed receptu), občas
# si "spletl" kus vlastní instrukce s odpovědí – takový titulek raději zahoď.
_SUSPICIOUS_TITLE_RE = re.compile(r"\bJSON\b|\bPOUZE\b|\bODPOVĚZ\b", re.I)

_PROMPT = (
    "Toto je fotografie ÚSEKU papírového nebo rukou psaného receptu (může jít "
    "jen o část delšího receptu, fotografovaného po kouscích odshora dolů). "
    "Vytáhni z něj: název receptu (pokud je na tomto úseku vidět, jinak prázdný "
    "řetězec), seznam surovin PŘESNĚ tak, jak jsou napsané (množství i "
    "jednotka spolu s názvem na jednom řádku), a text postupu přípravy "
    "(pokud je na tomto úseku vidět, jinak prázdný řetězec). Pokud recept má "
    "oddělené části (např. těsto/náplň/poleva/krém), vlož před suroviny dané "
    "části samostatný řádek s jejím názvem a dvojtečkou (např. 'Poleva:'), "
    "ať je vidět, kam patří. Odpověz POUZE "
    'JSON {"title": string, "ingredients": [string], "instructions": string}.'
)


def _extract_segment(image_bytes: bytes) -> dict:
    if not settings.ocr_model:
        raise RuntimeError("OCR model není nastaven (Admin → Nástroje → OCR model).")
    b64 = base64.b64encode(preprocess_image(image_bytes)).decode()
    out = chat_json(
        settings.ollama_url,
        settings.ocr_model,
        _PROMPT,
        images=[b64],
        timeout=max(settings.http_timeout, 120),
    )
    if out is None:
        log.warning("OCR receptu: volání modelu selhalo nebo odpověď nešla naparsovat.")
        return {"title": "", "ingredients": [], "instructions": ""}
    result = {
        "title": str(out.get("title") or "").strip(),
        "ingredients": [str(x).strip() for x in out.get("ingredients", []) if str(x).strip()],
        "instructions": str(out.get("instructions") or "").strip(),
    }
    if result["title"] and _SUSPICIOUS_TITLE_RE.search(result["title"]):
        log.warning("OCR receptu: zahozen podezřelý název (echo instrukce): %r", result["title"])
        result["title"] = ""
    if not result["title"] and not result["ingredients"]:
        log.warning("OCR receptu vrátil prázdný, ale validní výsledek (model nic nerozpoznal).")
    return result


def _structure_line(raw: str) -> dict:
    """Rychlý regex rozklad (bez LLM) pro editovatelný náhled – množství,
    jednotka a název zvlášť. Nadpisy sekcí (např. 'Poleva:') vyjdou jako
    řádek bez množství, s celým textem v 'name' – uživatel je pozná a může
    smazat nebo nechat jako oddělovač."""
    amount, unit, name = parse_line_regex(raw)
    return {"raw_text": raw, "amount": amount, "unit": unit, "name": name or raw}


def extract_draft(images: list[bytes]) -> dict:
    """Zpracuj všechny úseky a slož je do jednoho konceptu receptu."""
    segments = [_extract_segment(img) for img in images]
    title = next((s["title"] for s in segments if s["title"]), "")
    ingredient_lines = merge_lists([s["ingredients"] for s in segments])
    instructions = merge_texts([s["instructions"] for s in segments])

    image_url = save_recipe_photo(images[0]) if images else None

    return {
        "title": title,
        "ingredients": [_structure_line(ln) for ln in ingredient_lines],
        "instructions": instructions,
        "image_url": image_url,
    }
