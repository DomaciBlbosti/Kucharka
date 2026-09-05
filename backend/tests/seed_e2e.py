"""Malá databáze pro průchodový test v prohlížeči (viz frontend/e2e).

Spouští se z CI před startem appky. Data jsou schválně minimální, ale musí
pokrýt to, co test klika: recepty s tagy, obecnou i konkrétnější surovinu
(rýže / arborio rýže) a dost receptů na to, aby šlo poznat, že se výpis
opravdu naplnil.

    python -m tests.seed_e2e /cesta/k/e2e.db
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/kucharka-e2e.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Ingredient, Recipe, RecipeIngredient, RecipeTag, Tag,
)

INSTR = (
    "Rýži propláchneme a uvaříme v osolené vodě. Na oleji osmahneme cibuli, "
    "přidáme rýži a zalijeme vývarem. Vaříme 20 minut a promícháme."
)


def main() -> None:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        ryze = Ingredient(name_cs="rýže", category_path="obiloviny a pečivo > rýže")
        arborio = Ingredient(name_cs="arborio rýže",
                             category_path="obiloviny a pečivo > rýže")
        db.add_all([ryze, arborio])
        db.flush()
        # Rodičovská vazba – test ověřuje, že „rýže" najde i arborio.
        arborio.parent_id = ryze.id

        hlavni = Tag(namespace="chod", slug="hlavni-jidlo", label_cs="Hlavní jídlo")
        vecere = Tag(namespace="chod", slug="vecere", label_cs="Večeře")
        db.add_all([hlavni, vecere])
        db.flush()

        # Názvy musí být opravdu různé, jinak je slučování variant sloučí
        # do jedné karty (title_key je množina kmenů názvu).
        titles = [
            "Rizoto s dýní", "Kuřecí kari", "Zeleninová polévka", "Čočkový salát",
            "Bramborák na plechu", "Špagety s pestem", "Dušená mrkev",
            "Pečený losos", "Houbová omáčka", "Jablečný závin",
            "Tvarohové knedlíky", "Fazolový guláš",
        ]
        for i, title in enumerate(titles):
            r = Recipe(
                title=title, source_url=f"https://web.cz/{i}", source_domain="web.cz",
                instructions=INSTR, rating=4.5 - i * 0.1, rating_count=20 + i,
                feed_score=5.0 - i * 0.1, ing_total=1, total_time=30 + i,
                kcal_per_serving=400 + i * 10,
            )
            db.add(r)
            db.flush()
            db.add(RecipeIngredient(
                recipe_id=r.id, raw_text="200 g rýže", amount=200, unit="g",
                ingredient_id=(arborio.id if i % 2 else ryze.id),
            ))
            db.add(RecipeTag(recipe_id=r.id,
                             tag_id=(hlavni.id if i % 3 else vecere.id)))
        db.commit()
        print(f"e2e databáze připravena: {DB_PATH} ({len(titles)} receptů)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
