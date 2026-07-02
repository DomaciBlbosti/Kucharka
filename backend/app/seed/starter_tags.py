"""Kanonické tagy receptů – šest jmenných prostorů, ~50 tagů.

Slouží k filtrování receptů. Recept může mít víc tagů z jednoho i více
prostorů zároveň (dušené kuřecí kari = technika:duseni + kuchyne:asijska +
chod:hlavni-jidlo + denni-doba:vecere).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Tag

# (namespace, slug, label_cs)
STARTER: list[tuple[str, str, str]] = [
    # chod
    ("chod", "predkrm", "Předkrm"),
    ("chod", "polevka", "Polévka"),
    ("chod", "hlavni-jidlo", "Hlavní jídlo"),
    ("chod", "priloha", "Příloha"),
    ("chod", "salat", "Salát"),
    ("chod", "dezert", "Dezert"),
    ("chod", "napoj", "Nápoj"),
    ("chod", "pomazanka", "Pomazánka"),
    # denní doba / příležitost
    ("denni-doba", "snidane", "Snídaně"),
    ("denni-doba", "svacina", "Svačina"),
    ("denni-doba", "obed", "Oběd"),
    ("denni-doba", "vecere", "Večeře"),
    ("denni-doba", "brunch", "Brunch"),
    ("denni-doba", "oslava", "Oslava/party"),
    # chuť
    ("chut", "sladke", "Sladké"),
    ("chut", "slane", "Slané"),
    ("chut", "pikantni", "Pikantní"),
    ("chut", "kysele", "Kyselé"),
    ("chut", "korenite", "Kořeněné"),
    ("chut", "jemne", "Jemné"),
    ("chut", "uzene", "Uzené"),
    # technika přípravy
    ("technika", "peceni", "Pečení"),
    ("technika", "vareni", "Vaření"),
    ("technika", "smazeni", "Smažení"),
    ("technika", "grilovani", "Grilování"),
    ("technika", "duseni", "Dušení"),
    ("technika", "syrove", "Syrové / bez vaření"),
    ("technika", "pomale-vareni", "Pomalé vaření"),
    ("technika", "jedna-panev", "Jedna pánev/hrnec"),
    ("technika", "na-parou", "Na páře"),
    # dieta / omezení
    ("dieta", "vegetarianske", "Vegetariánské"),
    ("dieta", "veganske", "Veganské"),
    ("dieta", "bezlepkove", "Bezlepkové"),
    ("dieta", "bez-laktozy", "Bez laktózy"),
    ("dieta", "nizkosacharidove", "Nízkosacharidové"),
    ("dieta", "vysokoproteinove", "Vysokoproteinové"),
    ("dieta", "fitness", "Fitness/light"),
    ("dieta", "bez-cukru", "Bez cukru"),
    ("dieta", "bez-orechu", "Bez ořechů"),
    # kuchyně
    ("kuchyne", "ceska", "Česká"),
    ("kuchyne", "italska", "Italská"),
    ("kuchyne", "asijska", "Asijská"),
    ("kuchyne", "mexicka", "Mexická"),
    ("kuchyne", "francouzska", "Francouzská"),
    ("kuchyne", "indicka", "Indická"),
    ("kuchyne", "mediteranska", "Mediteránská"),
    ("kuchyne", "americka", "Americká"),
    ("kuchyne", "balkanska", "Balkánská"),
    ("kuchyne", "blizkovychodni", "Blízkovýchodní"),
]

NAMESPACE_LABELS = {
    "chod": "Chod",
    "denni-doba": "Denní doba",
    "chut": "Chuť",
    "technika": "Technika přípravy",
    "dieta": "Dieta / omezení",
    "kuchyne": "Kuchyně",
}


def seed_tags(db: Session) -> int:
    """Vlož chybějící kanonické tagy. Vrátí počet nově vytvořených."""
    existing = {
        (ns, slug) for ns, slug in db.execute(select(Tag.namespace, Tag.slug)).all()
    }
    created = 0
    for ns, slug, label in STARTER:
        if (ns, slug) not in existing:
            db.add(Tag(namespace=ns, slug=slug, label_cs=label))
            created += 1
    if created:
        db.commit()
    return created
