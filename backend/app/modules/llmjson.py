"""Robustní parsování JSON odpovědi z Ollamy.

I s "think": False se občas model odchýlí – zabalí odpověď do markdown code
fence, nebo (u modelů, které "think" nejdou úplně vypnout) nechá v textu
zbytek <think>...</think> bloku. Tohle to očistí předtím, než se zkusí
naparsovat jako JSON, ať appka nedostane tiše prázdný výsledek jen proto,
že kolem skutečné odpovědi bylo pár znaků navíc.
"""
from __future__ import annotations

import json
import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json_response(text: str) -> dict:
    cleaned = _THINK_RE.sub("", text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    return json.loads(cleaned)
