"""Sloučení seznamů textových řádků z více na sebe navazujících úseků
(fotek). Používá se pro účtenky i pro recepty vyfocené po částech –
překryv mezi sousedními úseky se odstraňuje fuzzy porovnáním konce
jednoho seznamu se začátkem druhého (ne pixelovým sešíváním fotek)."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_CHECK = 5  # kolik posledních/prvních položek na hranici porovnávat


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _find_overlap(prev: list, nxt: list, key) -> int:
    limit = min(_CHECK, len(prev), len(nxt))
    for k in range(limit, 0, -1):
        tail, head = prev[-k:], nxt[:k]
        if all(fuzz.ratio(norm(key(a)), norm(key(b))) >= 80 for a, b in zip(tail, head)):
            return k
    return 0


def merge_items(segments: list[list], key=lambda x: x) -> list:
    """Obecná verze merge_lists – funguje i nad seznamy ne-stringových položek
    (např. dvojic (množství, název)), porovnání překryvu jede přes `key`."""
    merged: list = []
    for seg in segments:
        seg = [s for s in seg if key(s) and key(s).strip()]
        if not merged:
            merged.extend(seg)
            continue
        merged.extend(seg[_find_overlap(merged, seg, key) :])
    return merged


def merge_lists(segments: list[list[str]]) -> list[str]:
    """Slož seznamy položek z po sobě jdoucích úseků a odstraň překryv na hranicích."""
    return merge_items(segments, key=lambda x: x)


def merge_texts(blocks: list[str]) -> str:
    """Totéž pro víceřádkový text (např. postup) – sloučí po řádcích."""
    line_lists = [b.splitlines() for b in blocks if b and b.strip()]
    return "\n".join(merge_lists(line_lists))
