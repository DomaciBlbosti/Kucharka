"""Zpracování receptu v jádru: parsování názvu suroviny, fuzzy párování proti
slovníku z webu a sestavení ingest payloadu."""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz, process

_UNITS = r"(?:kg|dkg|dag|mg|ml|dl|ks|kus|kusy|lžíce|lžička|lžičky|hrnek|hrnky|" \
         r"stroužek|stroužky|špetka|balení|plátek|plátky|snítka|hrst|konzerva|" \
         r"sáček|kostka|listy|list|g|l)"
_AMOUNT = re.compile(r"^\s*[\d.,/½¼¾–\-\s]*\s*(?:" + _UNITS + r"\b\.?\s*)?", re.I)
_PARENS = re.compile(r"\([^)]*\)")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def parse_name(raw: str) -> str:
    """Z 'raw' řádku odhadni čistý název suroviny (bez množství/jednotky)."""
    txt = _PARENS.sub("", raw or "")
    txt = _AMOUNT.sub("", txt)
    txt = txt.split(",")[0]
    return txt.strip(" .-–—")[:120].strip()


class Matcher:
    """Slovník z webu → mapa normalizovaný název/alias → kanonický name_cs."""

    def __init__(self, dictionary: list[dict]):
        self.map: dict[str, str] = {}
        self.names: list[str] = []
        for ing in dictionary:
            name = ing["name_cs"]
            self.names.append(name)
            self.map[_norm(name)] = name
            for a in ing.get("aliases", []):
                self.map[_norm(a)] = name
        self._norm_names = {_norm(n): n for n in self.names}

    def match(self, name: str) -> str:
        """Vrať kanonický název (existující), nebo očištěný název pro nový záznam."""
        key = _norm(name)
        if not key:
            return name
        if key in self.map:
            return self.map[key]
        # fuzzy proti kanonickým názvům
        cand = process.extractOne(key, self._norm_names.keys(), scorer=fuzz.WRatio)
        if cand and cand[1] >= 90:
            return self._norm_names[cand[0]]
        return name  # nový – web ho vytvoří


def build_payload(scraped: dict, matcher: Matcher) -> dict:
    lines = scraped.get("ingredients", []) or []
    ingredients = []
    for raw in lines:
        nm = parse_name(raw)
        canonical = matcher.match(nm) if nm else None
        ingredients.append({
            "raw_text": raw[:400],
            "ingredient_name": canonical or nm or None,
        })
    return {
        "title": scraped["title"],
        "source_url": scraped["source_url"],
        "source_domain": scraped.get("source_domain"),
        "image_url": scraped.get("image_url"),
        "instructions": scraped.get("instructions"),
        "servings": scraped.get("servings"),
        "total_time": scraped.get("total_time"),
        "rating": scraped.get("rating"),
        "rating_count": scraped.get("rating_count"),
        "ingredients": ingredients,
    }


def looks_czech(domain: str | None, text: str) -> bool:
    if domain and domain.endswith(".cz"):
        return True
    return any(c in set("ěščřžůňďť") for c in (text or "")[:2000].lower())
