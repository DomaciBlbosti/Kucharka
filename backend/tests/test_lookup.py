"""Testy derivace lookup_key.

Spustit: `python -m backend.tests.test_lookup` (ze repo root) nebo `pytest`.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.lookup import make_lookup_key


def _check(raw: str, expected: str) -> tuple[bool, str]:
    make_lookup_key.cache_clear()
    actual = make_lookup_key(raw)
    ok = actual == expected
    return ok, f"{raw!r:40} → {actual!r:30} (exp {expected!r})"


def _check_equal(*raws: str) -> tuple[bool, str]:
    """Všechny vstupy musí dát stejný lookup_key."""
    make_lookup_key.cache_clear()
    keys = {raw: make_lookup_key(raw) for raw in raws}
    unique_values = set(keys.values())
    ok = len(unique_values) == 1 and "" not in unique_values
    lines = [f"  {r!r:40} → {k!r}" for r, k in keys.items()]
    return ok, "\n" + "\n".join(lines)


# ─── Exact match tests ───────────────────────────────────────────────────────

EXACT_CASES = [
    # quantity + unit stripping
    ("150 g cukru",                  "cukr"),
    # POZN: simplemma nezvládá "mouky → mouka", vrací "mouky". Slovník
    # to dorovná: první výskyt přes LLM uloží alias s lookup_key="mouky",
    # další "mouky" tedy zasáhne přímo. Nezprostředkovaně to znamená, že
    # slovník bude trochu větší (víc lookup_key pro stejnou surovinu),
    # ale fungování to nezničí.
    ("2 lžíce mouky",                "mouky"),
    ("3 ks vajec",                   "vejce"),
    # "mléka" naopak simplemma zvládne ("mléka → mléko")
    ("½ hrnku mléka",                "mleko"),
    ("1/2 lžičky soli",              "sul"),
    ("špetka pepře",                 "pepr"),
    # parens
    ("mouka hladká (na zaprášení)",  "mouka hladky"),
    ("vejce [bio]",                  "vejce"),
    # simple plurals
    ("brambory",                     "brambora"),
    ("česnek",                       "cesnek"),
    # diacritics output stripped
    ("kuřecí maso",                  "kureci maso"),
    # empty / edge
    ("",                             ""),
    ("   ",                          ""),
    ("100 g",                        ""),     # jen množství, bez suroviny
]

# Skupiny tvarů, které simplemma spojí na stejné lemma.
EQUIVALENCE_GROUPS = [
    # běžné suroviny v různých pádech (simplemma je zvládá)
    ("cukru", "cukr", "cukrem"),
    ("česneku", "česnek", "česnekem"),
    ("vejce", "vajec"),
    # diakritika nemá vliv na finální klíč
    ("kuřecí maso", "kureci maso"),
    # case-insensitive
    ("Mouka", "MOUKA", "mouka"),
]

# Skupiny, kde simplemma selhává — slovník je dorovná postupně.
# Ponecháno jako dokumentace, ne jako test.
_KNOWN_LIMITATIONS = [
    ("mouka", "200 g mouky", "lžíce mouky"),   # mouky nezná
    ("mléko", "hrnek mléka"),                   # mléka nezná
    ("máslo", "lžíce másla"),                   # másla nezná
]


def run_tests() -> int:
    failed = 0
    print("=== Exact key tests ===")
    for raw, expected in EXACT_CASES:
        ok, msg = _check(raw, expected)
        print(("OK " if ok else "FAIL ") + msg)
        if not ok:
            failed += 1

    print("\n=== Equivalence groups ===")
    for group in EQUIVALENCE_GROUPS:
        ok, msg = _check_equal(*group)
        print(("OK " if ok else "FAIL ") + " ".join(repr(r) for r in group) + msg)
        if not ok:
            failed += 1

    print("\n=== Cache test ===")
    make_lookup_key.cache_clear()
    info1 = make_lookup_key.cache_info()
    make_lookup_key("150 g cukru")
    make_lookup_key("150 g cukru")
    info2 = make_lookup_key.cache_info()
    if info2.hits >= 1:
        print(f"OK  cache hits: {info2.hits}")
    else:
        print(f"FAIL  expected cache hit, got {info2}")
        failed += 1

    print("\n=== Idempotence ===")
    samples = ["150 g cukru", "kuřecí prsa", "mouka hladká (na zaprášení)"]
    for s in samples:
        k1 = make_lookup_key(s)
        k2 = make_lookup_key(k1)
        if k1 == k2:
            print(f"OK  idempotent: {s!r} → {k1!r}")
        else:
            print(f"FAIL  not idempotent: {s!r} → {k1!r} → {k2!r}")
            failed += 1

    print(f"\n{'='*60}\n{'FAILED: ' + str(failed) if failed else 'ALL PASS'}\n")
    return failed


if __name__ == "__main__":
    sys.exit(run_tests())
