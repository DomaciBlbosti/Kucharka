"""Seed kanonického setu tagů. Idempotentní — opakované spuštění je no-op."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Tag

# (namespace, slug, label_cs)
SEED_TAGS: tuple[tuple[str, str, str], ...] = (
    # course — co to v zásadě je
    ("course", "main",       "Hlavní jídlo"),
    ("course", "soup",       "Polévka"),
    ("course", "appetizer",  "Předkrm"),
    ("course", "salad",      "Salát"),
    ("course", "dessert",    "Dezert"),
    ("course", "drink",      "Nápoj"),
    ("course", "side",       "Příloha"),
    ("course", "sauce",      "Omáčka / dip"),
    ("course", "preserve",   "Zavařování"),
    ("course", "pastry",     "Pečivo"),

    # flavor — chuť
    ("flavor", "sweet",      "Sladké"),
    ("flavor", "savory",     "Slané"),
    ("flavor", "spicy",      "Pálivé"),
    ("flavor", "sour",       "Kyselé"),

    # meal — kdy se jí
    ("meal", "breakfast",    "Snídaně"),
    ("meal", "lunch",        "Oběd"),
    ("meal", "dinner",       "Večeře"),
    ("meal", "snack",        "Svačina"),
    ("meal", "brunch",       "Brunch"),

    # technique — způsob přípravy
    ("technique", "baking",       "Pečení"),
    ("technique", "frying",       "Smažení"),
    ("technique", "grilling",     "Grilování"),
    ("technique", "slow_cooking", "Pomalé vaření"),
    ("technique", "roasting",     "Pečení v troubě"),
    ("technique", "raw",          "Tepelně neupravené"),
    ("technique", "boiling",      "Vaření"),
    ("technique", "steaming",     "V páře"),
    ("technique", "fermenting",   "Fermentace"),
    ("technique", "no_cook",      "Bez vaření"),

    # diet — dietní vhodnost (konzervativně přidělováno)
    ("diet", "vegan",         "Vegan"),
    ("diet", "vegetarian",    "Vegetariánské"),
    ("diet", "gluten_free",   "Bezlepkové"),
    ("diet", "lactose_free",  "Bezlaktózové"),
    ("diet", "low_carb",      "Nízkosacharidové"),
    ("diet", "high_protein",  "Vysokoproteinové"),

    # cuisine — odkud
    ("cuisine", "czech",          "Česká"),
    ("cuisine", "slovak",         "Slovenská"),
    ("cuisine", "indian",         "Indická"),
    ("cuisine", "thai",           "Thajská"),
    ("cuisine", "chinese",        "Čínská"),
    ("cuisine", "japanese",       "Japonská"),
    ("cuisine", "korean",         "Korejská"),
    ("cuisine", "vietnamese",     "Vietnamská"),
    ("cuisine", "italian",        "Italská"),
    ("cuisine", "french",         "Francouzská"),
    ("cuisine", "mexican",        "Mexická"),
    ("cuisine", "greek",          "Řecká"),
    ("cuisine", "middle_eastern", "Středovýchodní"),
    ("cuisine", "mediterranean",  "Středomořská"),
    ("cuisine", "american",       "Americká"),
    ("cuisine", "british",        "Britská"),
    ("cuisine", "international",  "Mezinárodní"),
)


def seed_tags(db: Session) -> int:
    """Doplň chybějící tagy. Vrátí počet přidaných."""
    existing = {(t.namespace, t.slug) for t in db.scalars(select(Tag)).all()}
    added = 0
    for namespace, slug, label_cs in SEED_TAGS:
        if (namespace, slug) in existing:
            continue
        db.add(Tag(namespace=namespace, slug=slug, label_cs=label_cs))
        added += 1
    if added:
        db.commit()
    return added
