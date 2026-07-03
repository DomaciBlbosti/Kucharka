"""Import receptu z fotek papírového/rukou psaného receptu.

Recept může být vyfocený po částech (více úseků/stránek); z každého úseku
vytáhneme přes vision model v Ollamě název, suroviny a postup a slučujeme
je stejnou fuzzy-merge logikou jako u účtenek (viz textmerge.py). Uložení
konceptu pak jede přes existující ingest pipeli (stejnou jako u receptů
stažených z webu) – suroviny se tak normalizují a párují identicky.

Proč "doslovný přepis" místo strukturované extrakce
----------------------------------------------------
Dřív jsme model žádali, ať suroviny rovnou strukturuje (jméno + množství
zvlášť, případně dva paralelní seznamy). Ukázalo se (opakovaně, na stejné
fotce, deterministicky), že menší vision model tohle neumí spolehlivě –
strukturovaný multi-pole JSON dost často zredukuje na pouhé názvy a čísla u
surovin prostě vynechá, i když je na fotce jasně vidí a jinde v odpovědi je
zvládne přepsat (např. "2 lžíce kakaa" → vrátí jen "lžíce kakao"). OCR/čtení
textu samo o sobě mu problém nedělá – problém je až "zorganizuj to do
schématu".

Proto teď žádáme jen DOSLOVNÝ PŘEPIS řádků (jednodušší úkol, blíž tomu, na
co jsou OCR modely trénované), a množství/jednotku z každého řádku vytahuje
až spolehlivý Python regex (`parse_line_regex`), ne model. Two-column recepty
(množství vlevo, surovina vpravo) navíc necháváme sloučit do jednoho řádku
už v promptu; pro případ, že by to model přesto rozsypal na dva řádky,
`_merge_split_lines` dodatečně spáruje řádek s "holým" množstvím a
následující řádek s "holým" názvem.
"""
from __future__ import annotations

import base64
import logging
import re

from ..config import settings
from .normalizer import parse_line_regex
from .ollamachat import chat_json_raw
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
    "Přepiš DOSLOVA text surovin, tak jak je vidíš – žádné číslo, jednotku "
    "ani slovo nevynechávej a nic si nedomýšlej. Recepty bývají psané ve "
    "dvou sloupcích (množství vlevo, surovina vpravo na stejném řádku) – "
    "takový řádek přepiš jako JEDEN řetězec 'množství surovina', např. "
    "'2 lžíce kakaa' nebo '10 dkg mouka'. Pokud u suroviny množství není "
    "napsané, přepiš jen její název. Pokud recept má oddělené části (např. "
    "těsto/náplň/poleva/krém), vlož jako samostatnou položku název té části "
    "s dvojtečkou (např. 'Poleva:'). Každá surovina/nadpis = jedna položka "
    "pole 'ingredient_lines', v pořadí shora dolů, jak jdou na fotce. Dál "
    "vytáhni: název receptu (title, pokud je na tomto úseku vidět, jinak "
    "prázdný řetězec) a text postupu přípravy (instructions, pokud je na "
    "tomto úseku vidět, jinak prázdný řetězec). Odpověz POUZE JSON "
    '{"title": string, "ingredient_lines": [string], "instructions": string}.'
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
        return {"title": "", "ingredients": [], "instructions": "", "debug_raw": raw}

    result = {
        "title": str(out.get("title") or "").strip(),
        "ingredients": [str(x).strip() for x in out.get("ingredient_lines", []) if str(x).strip()],
        "instructions": str(out.get("instructions") or "").strip(),
        "debug_raw": raw,
    }
    if result["title"] and _SUSPICIOUS_TITLE_RE.search(result["title"]):
        log.warning("OCR receptu: zahozen podezřelý název (echo instrukce): %r", result["title"])
        result["title"] = ""
    if not result["title"] and not result["ingredients"]:
        log.warning("OCR receptu vrátil prázdný, ale validní výsledek (model nic nerozpoznal).")
    return result


def _merge_split_lines(lines: list[str]) -> list[str]:
    """Pojistka pro případ, že model i přes instrukci v promptu rozsype
    dvousloupcový řádek na dvě položky – "holé množství" (např. '10 dkg',
    bez zbylého textu po regexu) hned následované "holým názvem" (bez
    čísla) se spojí do jednoho řádku."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if i + 1 < len(lines):
            amt, _unit, name = parse_line_regex(cur)
            nxt_amt, _nxt_unit, nxt_name = parse_line_regex(lines[i + 1])
            if amt is not None and not name and nxt_amt is None and nxt_name:
                out.append(f"{cur} {lines[i + 1]}".strip())
                i += 2
                continue
        out.append(cur)
        i += 1
    return out


def _structure_line(raw: str) -> dict:
    amount, unit, name = parse_line_regex(raw)
    return {"raw_text": raw, "amount": amount, "unit": unit, "name": name or raw}


def extract_draft(images: list[bytes]) -> dict:
    """Zpracuj všechny úseky a slož je do jednoho konceptu receptu."""
    segments = [_extract_segment(img) for img in images]
    title = next((s["title"] for s in segments if s["title"]), "")
    ingredient_lines = merge_lists([s["ingredients"] for s in segments])
    ingredient_lines = _merge_split_lines(ingredient_lines)
    instructions = merge_texts([s["instructions"] for s in segments])

    image_url = save_recipe_photo(images[0]) if images else None

    return {
        "title": title,
        "ingredients": [_structure_line(ln) for ln in ingredient_lines],
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
