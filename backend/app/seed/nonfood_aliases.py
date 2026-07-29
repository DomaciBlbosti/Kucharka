"""Builtin slovník notoricky známých ne-surovin.

Věci jako alobal nebo pečicí papír se v receptech objevují tisíckrát a nemá
smysl, aby se na ně každá instalace ptala LLM – zapíšou se do slovníku
aliasů rovnou při startu (kind != 'food' → enrichment i párování je přeskočí
a nepočítají se do kalorií). Idempotentní: existující klíče se nepřepisují.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ..models import IngredientAlias
from ..modules.lookup import make_lookup_key

# (text, kind) – kind: packaging (obal), equipment (náčiní)
NONFOOD = [
    ("alobal", "packaging"),
    ("pečící papír", "packaging"),
    ("pečicí papír", "packaging"),
    ("papír na pečení", "packaging"),
    ("potravinářská fólie", "packaging"),
    ("potravinová fólie", "packaging"),
    ("fresh fólie", "packaging"),
    ("mikrotenový sáček", "packaging"),
    ("špejle", "equipment"),
    ("párátko", "equipment"),
    ("párátka", "equipment"),
    ("papírové košíčky", "equipment"),
    ("košíčky na muffiny", "equipment"),
    ("silikonová forma", "equipment"),
    ("forma na bábovku", "equipment"),
    ("forma na pečení", "equipment"),
    ("mřížka na chlazení", "equipment"),
    ("cukrářský sáček", "equipment"),
    ("zdobicí sáček", "equipment"),
]


def seed_nonfood(db) -> int:
    existing_keys = set(
        db.scalars(
            select(IngredientAlias.lookup_key).where(IngredientAlias.lookup_key.is_not(None))
        ).all()
    )
    added = 0
    for text, kind in NONFOOD:
        key = make_lookup_key(text)
        if not key or key in existing_keys:
            continue
        db.add(IngredientAlias(
            alias=key[:200], lookup_key=key[:200], ingredient_id=None,
            kind=kind, source="builtin", confidence=1.0,
            verified=True, verified_at=datetime.utcnow(),
        ))
        existing_keys.add(key)
        added += 1
    if added:
        db.commit()
    return added
