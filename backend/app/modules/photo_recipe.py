"""Import receptu z fotek papírového/rukou psaného receptu.

Recept může být vyfocený po částech (více úseků/stránek); z každého úseku
vytáhneme přes vision model v Ollamě název, suroviny a postup a slučujeme
je stejnou fuzzy-merge logikou jako u účtenek (viz textmerge.py). Uložení
konceptu pak jede přes existující ingest pipeli (stejnou jako u receptů
stažených z webu) – suroviny se tak normalizují a párují identicky.

Suroviny a jejich množství chodí z vision modelu jako DVA paralelní seznamy
("ingredients" a "quantities"), párované indexem – ne jako jeden plochý text
na řádek. Důvod: papírové recepty bývají psané ve dvou sloupcích (množství
vlevo, surovina vpravo) a model při čtení "po řádcích" tenhle sloupcový
layout dost často rozsype na dvě samostatné položky v jednom seznamu (např.
"10 dkg" a "mouka" jako dvě různé položky) – pak nejde nic spárovat zpětně
regexem, protože položka s množstvím žádný text suroviny neobsahuje a naopak.
Se dvěma seznamy tenhle problém odpadá, i kdyby model četl sloupce
"nezávisle" – pár (i-té množství, i-tá surovina) sedí podle pozice v řádku.
"""
from __future__ import annotations

import base64
import logging
import re

from ..config import settings
from .normalizer import parse_line_regex
from .ollamachat import chat_json_raw
from .receipt import preprocess_image  # sdílené zmenšení/oříznutí podle EXIF
from .textmerge import merge_items, merge_texts
from .uploads import save_recipe_photo

log = logging.getLogger("kucharka.photo_recipe")

# Když model na fotce nenajde název (typicky úsek uprostřed receptu), občas
# si "spletl" kus vlastní instrukce s odpovědí – takový titulek raději zahoď.
_SUSPICIOUS_TITLE_RE = re.compile(r"\bJSON\b|\bPOUZE\b|\bODPOVĚZ\b", re.I)

_PROMPT = (
    "Toto je fotografie ÚSEKU papírového nebo rukou psaného receptu (může jít "
    "jen o část delšího receptu, fotografovaného po kouscích odshora dolů). "
    "Recepty bývají psané ve dvou sloupcích – množství vlevo, surovina vpravo "
    "na stejném řádku. Vytáhni z něj: název receptu (pokud je na tomto úseku "
    "vidět, jinak prázdný řetězec), suroviny a text postupu přípravy (pokud "
    "je na tomto úseku vidět, jinak prázdný řetězec). Suroviny vrať jako DVA "
    "seznamy stejné délky, položku po položce PODLE ŘÁDKŮ (ne podle sloupců): "
    "'ingredients' – jen název suroviny bez množství, v 1. pádě; "
    "'quantities' – jen množství a jednotka k dané surovině přesně jak jsou "
    "napsané (prázdný řetězec, pokud u té suroviny množství není). i-tá "
    "položka 'quantities' patří k i-té položce 'ingredients'. Pokud recept "
    "má oddělené části (např. těsto/náplň/poleva/krém), vlož do 'ingredients' "
    "samostatnou položku s názvem té části a dvojtečkou (např. 'Poleva:') a "
    "k ní prázdné množství. Odpověz POUZE JSON "
    '{"title": string, "ingredients": [string], "quantities": [string], '
    '"instructions": string}.'
)


def _extract_segment(image_bytes: bytes) -> dict:
    if not settings.ocr_model:
        raise RuntimeError("OCR model není nastaven (Admin → Nástroje → OCR model).")
    b64 = base64.b64encode(preprocess_image(image_bytes)).decode()
    out, raw = chat_json_raw(
        settings.ollama_url,
        settings.ocr_model,
        _PROMPT,
        images=[b64],
        timeout=max(settings.http_timeout, 120),
    )
    if out is None:
        log.warning("OCR receptu: volání modelu selhalo nebo odpověď nešla naparsovat.")
        return {"title": "", "pairs": [], "instructions": "", "debug_raw": raw}

    names = [str(x).strip() for x in out.get("ingredients", [])]
    qtys = [str(x).strip() for x in out.get("quantities", [])]
    if len(qtys) < len(names):
        qtys += [""] * (len(names) - len(qtys))  # model zapomněl doplnit "" k některým položkám
    pairs = [(qtys[i], names[i]) for i in range(len(names)) if names[i]]

    result = {
        "title": str(out.get("title") or "").strip(),
        "pairs": pairs,
        "instructions": str(out.get("instructions") or "").strip(),
        "debug_raw": raw,
    }
    if result["title"] and _SUSPICIOUS_TITLE_RE.search(result["title"]):
        log.warning("OCR receptu: zahozen podezřelý název (echo instrukce): %r", result["title"])
        result["title"] = ""
    if not result["title"] and not result["pairs"]:
        log.warning("OCR receptu vrátil prázdný, ale validní výsledek (model nic nerozpoznal).")
    return result


def _structure_pair(qty: str, name: str) -> dict:
    """Množství (text z modelu, např. '10 dkg') rozlož regexem na
    amount/unit; název suroviny už máme přímo od modelu, nic dalšího z něj
    vytahovat netřeba."""
    amount, unit, leftover = parse_line_regex(qty) if qty else (None, None, "")
    full_name = f"{leftover} {name}".strip() if leftover else name
    raw_text = f"{qty} {name}".strip()
    return {"raw_text": raw_text, "amount": amount, "unit": unit, "name": full_name}


def extract_draft(images: list[bytes]) -> dict:
    """Zpracuj všechny úseky a slož je do jednoho konceptu receptu."""
    segments = [_extract_segment(img) for img in images]
    title = next((s["title"] for s in segments if s["title"]), "")
    pairs = merge_items([s["pairs"] for s in segments], key=lambda p: p[1])
    instructions = merge_texts([s["instructions"] for s in segments])

    image_url = save_recipe_photo(images[0]) if images else None

    return {
        "title": title,
        "ingredients": [_structure_pair(qty, name) for qty, name in pairs],
        "instructions": instructions,
        "image_url": image_url,
        # Syrová odpověď modelu za každý vyfocený úsek – jen pro debug náhled
        # v UI ("Recept z fotky" → Debug), ať jde vidět, co OCR skutečně vrátil,
        # aniž by bylo nutné hrabat se v server logu.
        "debug": {
            "model": settings.ocr_model,
            "segments": [s.get("debug_raw", "") for s in segments],
        },
    }
