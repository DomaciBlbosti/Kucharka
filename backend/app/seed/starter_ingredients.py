"""Základní sada surovin, aby appka fungovala hned po startu.

Hodnoty kcal/100 g jsou orientační. Reálná data doplníš importem z
NutriDatabaze.cz (viz seed/import_nutridb.py). density = g na 1 ml.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Ingredient

# (name_cs, name_en, category, kcal, protein, carbs, fat, fiber, density)
STARTER: list[tuple] = [
    ("mouka hladká", "wheat flour", "Mouky", 364, 10, 76, 1, 2.7, 0.6),
    ("mouka polohrubá", "wheat flour", "Mouky", 364, 10, 76, 1, 2.7, 0.6),
    ("cukr krystal", "sugar", "Sladidla", 400, 0, 100, 0, 0, 0.85),
    ("cukr moučka", "powdered sugar", "Sladidla", 400, 0, 100, 0, 0, 0.56),
    ("sůl", "salt", "Koření", 0, 0, 0, 0, 0, 1.2),
    ("vejce", "egg", "Vejce", 155, 13, 1.1, 11, 0, None),
    ("mléko", "milk", "Mléčné", 64, 3.4, 4.8, 3.6, 0, 1.03),
    ("máslo", "butter", "Tuky", 740, 0.9, 0.7, 82, 0, 0.91),
    ("olej", "oil", "Tuky", 884, 0, 0, 100, 0, 0.92),
    ("smetana ke šlehání", "cream", "Mléčné", 337, 2.5, 3, 35, 0, 1.0),
    ("zakysaná smetana", "sour cream", "Mléčné", 195, 2.5, 3.5, 19, 0, 1.0),
    ("cibule", "onion", "Zelenina", 40, 1.1, 9, 0.1, 1.7, None),
    ("česnek", "garlic", "Zelenina", 149, 6.4, 33, 0.5, 2.1, None),
    ("brambory", "potato", "Zelenina", 77, 2, 17, 0.1, 2.2, None),
    ("mrkev", "carrot", "Zelenina", 41, 0.9, 10, 0.2, 2.8, None),
    ("rajče", "tomato", "Zelenina", 18, 0.9, 3.9, 0.2, 1.2, None),
    ("paprika", "bell pepper", "Zelenina", 31, 1, 6, 0.3, 2.1, None),
    ("kuřecí prsa", "chicken breast", "Maso", 165, 31, 0, 3.6, 0, None),
    ("mleté hovězí", "ground beef", "Maso", 250, 26, 0, 15, 0, None),
    ("vepřová pečeně", "pork", "Maso", 242, 27, 0, 14, 0, None),
    ("slanina", "bacon", "Maso", 541, 37, 1.4, 42, 0, None),
    ("rýže", "rice", "Přílohy", 360, 7, 79, 0.6, 1.3, 0.85),
    ("těstoviny", "pasta", "Přílohy", 371, 13, 75, 1.5, 3, None),
    ("sýr eidam", "cheese", "Mléčné", 330, 26, 0, 25, 0, None),
    ("parmazán", "parmesan", "Mléčné", 431, 38, 4, 29, 0, None),
    ("rajčatový protlak", "tomato paste", "Konzervy", 82, 4, 19, 0.5, 4, 1.1),
    ("máslo arašídové", "peanut butter", "Tuky", 588, 25, 20, 50, 6, None),
    ("med", "honey", "Sladidla", 304, 0.3, 82, 0, 0.2, 1.42),
    ("kakao", "cocoa", "Pečení", 228, 20, 58, 14, 33, 0.5),
    ("droždí", "yeast", "Pečení", 105, 13, 12, 2, 0, None),
    ("kypřicí prášek", "baking powder", "Pečení", 53, 0, 28, 0, 0.2, None),
    ("citron", "lemon", "Ovoce", 29, 1.1, 9, 0.3, 2.8, None),
    ("jablko", "apple", "Ovoce", 52, 0.3, 14, 0.2, 2.4, None),
    ("banán", "banana", "Ovoce", 89, 1.1, 23, 0.3, 2.6, None),
    ("špenát", "spinach", "Zelenina", 23, 2.9, 3.6, 0.4, 2.2, None),
    ("houby žampiony", "mushroom", "Zelenina", 22, 3.1, 3.3, 0.3, 1, None),
    ("voda", "water", "Tekutiny", 0, 0, 0, 0, 0, 1.0),
]


def seed_starter(db: Session) -> int:
    """Vlož základní suroviny, jen pokud je tabulka prázdná."""
    if db.scalar(select(func.count(Ingredient.id))):
        return 0
    for row in STARTER:
        db.add(
            Ingredient(
                name_cs=row[0],
                name_en=row[1],
                category=row[2],
                kcal_100g=row[3],
                protein_100g=row[4],
                carbs_100g=row[5],
                fat_100g=row[6],
                fiber_100g=row[7],
                density=row[8],
                source="starter",
            )
        )
    db.commit()
    return len(STARTER)
