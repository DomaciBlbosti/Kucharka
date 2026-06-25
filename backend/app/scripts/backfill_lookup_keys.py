"""Jednorázový backfill `ingredient_alias.lookup_key` pro stávající řádky.

Pro každý alias bez `lookup_key` spočítá deterministický klíč. Detekuje
kolize (víc raw aliasů → stejný lookup_key); v takovém případě ponechá
alias s nejvyšším `hit_count` (tiebreak: nejnižší `id`). Ostatní zůstávají
v DB s `lookup_key=NULL` — nevadí, lookup je najde fallbackem podle `alias`.

Spuštění (uvnitř kontejneru):

    docker exec ix-kucharka-app-1 python -m app.scripts.backfill_lookup_keys

Idempotentní: druhé spuštění neudělá nic, protože všechny aliasy už mají klíč.
Po dokončení vypíše statistiky kolizí.
"""
from __future__ import annotations

import logging
import sys
from collections import defaultdict

from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import IngredientAlias
from ..modules.lookup import make_lookup_key

log = logging.getLogger("kucharka.backfill")


def run() -> dict:
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(IngredientAlias).where(IngredientAlias.lookup_key.is_(None))
        ).all()
        log.info("Backfill: %s aliasů bez lookup_key", len(rows))
        if not rows:
            return {"processed": 0, "updated": 0, "collisions": 0, "winners": 0}

        # Klíče, které už v DB existují (z předchozího běhu nebo z provozu)
        existing_keys = set(db.scalars(
            select(IngredientAlias.lookup_key).where(IngredientAlias.lookup_key.is_not(None))
        ).all())

        # Skupiny podle vypočteného klíče
        groups: dict[str, list[IngredientAlias]] = defaultdict(list)
        empty_count = 0
        for row in rows:
            key = make_lookup_key(row.alias)
            if not key:
                empty_count += 1
                continue
            groups[key].append(row)

        updated = 0
        collisions = 0
        winners = 0
        already_taken = 0
        for key, items in groups.items():
            if key in existing_keys:
                # Klíč už v DB obsadil dřívější záznam (nebo předchozí běh).
                # Tihle zůstanou bez lookup_key — lookup je najde fallbackem
                # přes alias kolonu.
                already_taken += 1
                log.info(
                    "Klíč %r už obsazený, %d kandidátů (%s) zůstává bez lookup_key",
                    key, len(items),
                    ", ".join(repr(a.alias) for a in items),
                )
                continue
            if len(items) == 1:
                items[0].lookup_key = key
                updated += 1
                continue
            # Kolize — vybrat vítěze
            collisions += 1
            winner = max(items, key=lambda a: ((a.hit_count or 0), -a.id))
            winner.lookup_key = key
            updated += 1
            winners += 1
            losers = [a.alias for a in items if a is not winner]
            log.info(
                "Kolize lookup_key=%r: %d kandidátů, vítěz alias=%r (hit_count=%s); "
                "ostatní (%s) zůstanou bez lookup_key",
                key, len(items), winner.alias, winner.hit_count,
                ", ".join(repr(l) for l in losers),
            )

        db.commit()
        result = {
            "processed": len(rows),
            "updated": updated,
            "empty_keys": empty_count,
            "collisions": collisions,
            "collision_winners": winners,
            "already_taken": already_taken,
        }
        log.info("Backfill hotov: %s", result)
        return result
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
    # Lazy init DB, ať jdeme za případnou výchozí konfigurací
    from ..main import init_db
    init_db()
    result = run()
    print(result)
    # Exit code 0 i při kolizích — to není chyba, jen info
    sys.exit(0)
