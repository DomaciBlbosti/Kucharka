"""Builtin slovník notoricky známých ne-surovin.

Věci jako alobal nebo pečicí papír se v receptech objevují tisíckrát a nemá
smysl, aby se na ně každá instalace ptala LLM – zapíšou se do slovníku
aliasů rovnou při startu (kind != 'food' → enrichment i párování je přeskočí
a nepočítají se do kalorií). Idempotentní: existující klíče se nepřepisují.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import IngredientAlias
from ..modules.lookup import make_lookup_key

log = logging.getLogger("kucharka.seed")

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
    """Idempotentní a odolné: kolize s existujícími záznamy (unikátní je jak
    `lookup_key`, tak `alias` – legacy záznamy mívají jen `alias`) se tiše
    přeskočí. Commit po JEDNOM řádku, ať jeden konflikt neshodí zbytek –
    a hlavně ať seed NIKDY nepoloží start aplikace."""
    seed_keys = [(make_lookup_key(t), kind) for t, kind in NONFOOD]
    seed_keys = [(k, kind) for k, kind in seed_keys if k]

    existing_keys = set(
        db.scalars(
            select(IngredientAlias.lookup_key).where(IngredientAlias.lookup_key.is_not(None))
        ).all()
    )
    # legacy záznamy bez lookup_key mají unikátní `alias` – kontrolu stačí
    # udělat jen pro našich pár seedovaných hodnot, ne načítat celý slovník
    wanted_aliases = [k[:200] for k, _ in seed_keys]
    existing_aliases = set(
        db.scalars(
            select(IngredientAlias.alias).where(IngredientAlias.alias.in_(wanted_aliases))
        ).all()
    )

    added = 0
    for key, kind in seed_keys:
        if key in existing_keys or key[:200] in existing_aliases:
            continue
        db.add(IngredientAlias(
            alias=key[:200], lookup_key=key[:200], ingredient_id=None,
            kind=kind, source="builtin", confidence=1.0,
            verified=True, verified_at=datetime.utcnow(),
        ))
        try:
            db.commit()
            existing_keys.add(key)
            added += 1
        except IntegrityError:
            db.rollback()  # souběžný/legacy duplikát – v pořádku, přeskočit
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.warning("seed ne-suroviny %r selhal, přeskakuji: %s", key, exc)
    return added
