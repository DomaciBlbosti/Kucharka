"""Ingest pipeline: URL → recept v DB.

scrape → normalize každý řádek → dopočet gramů a kcal → upsert receptu.
Idempotentní podle source_url (re-scrape aktualizuje hodnocení).

Instrumentace: každá fáze je změřená (`time.perf_counter`) a při dokončení
se do logu `kucharka.ingest.timing` vypíše rozpad času – ať je vidět, kde se
čas u receptu reálně tráví (síť vs. překlad vs. parsování vs. párování
surovin), než se cokoli optimalizuje. Loguje se na INFO.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Recipe, RecipeIngredient
from . import scraper, translate
from .normalizer import normalize_lines
from .nutrition import grams_for, kcal_for, recompute_recipe_kcal

log = logging.getLogger("kucharka.ingest.timing")


class _Timings:
    """Sesbírá časy fází pro jeden recept a na konci je vypíše do logu."""

    def __init__(self, url: str):
        self.url = url
        self.phases: dict[str, float] = {}
        self._t0 = time.perf_counter()

    @contextmanager
    def phase(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + (time.perf_counter() - start)

    def add(self, name: str, seconds: float) -> None:
        self.phases[name] = self.phases.get(name, 0.0) + seconds

    def dump(self, extra: str = "") -> None:
        total = time.perf_counter() - self._t0
        parts = " ".join(f"{k}={v:.2f}s" for k, v in self.phases.items())
        log.info("ingest %.2fs [%s]%s %s", total, parts, f" {extra}" if extra else "", self.url)


def ingest_url(db: Session, url: str) -> Recipe | None:
    t = _Timings(url)
    with t.phase("scrape"):
        data = scraper.fetch_and_extract(url)
    if data is None:
        t.dump("(nebyl recept)")
        return None
    with t.phase("translate"):
        data = translate.translate_recipe(data)  # cizí recept → čeština
    return _persist(db, data, t)


def _persist(db: Session, data: dict, t: _Timings | None = None) -> Recipe:
    if t is None:
        t = _Timings(data.get("source_url", "?"))

    # Pojistka: bez názvu recept neukládáme (title je NOT NULL a stejně by to
    # byl nepoužitelný záznam). Radši vrátit None → crawler to vezme jako skip,
    # než spadnout na IntegrityError a rozbít session.
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError(f"recept nemá název (title) – přeskočeno: {data.get('source_url')}")

    recipe = db.scalar(select(Recipe).where(Recipe.source_url == data["source_url"]))
    if recipe is None:
        # title musí být nastaven PŘED flush – je NOT NULL, jinak flush spadne
        # na IntegrityError dřív, než se vyplní zbytek polí níž.
        recipe = Recipe(source_url=data["source_url"], title=title)
        db.add(recipe)
        try:
            # Vynuť INSERT hned, ať odchytíme souběh: když stejnou URL právě
            # ukládá jiný požadavek (druhý uživatel, nebo crawler proti ručnímu
            # přidání), narazíme na UNIQUE(source_url) tady, ne až při commitu.
            db.flush()
        except IntegrityError:
            db.rollback()
            recipe = db.scalar(
                select(Recipe).where(Recipe.source_url == data["source_url"])
            )
            if recipe is None:
                # velmi nepravděpodobné (rollback smazal cizí řádek?) – radši
                # nevytvářet potenciální duplikát a dát to zpět volajícímu
                raise

    recipe.title = title
    recipe.source_domain = data.get("source_domain")
    recipe.image_url = data.get("image_url")
    recipe.video_url = data.get("video_url")
    recipe.instructions = data.get("instructions")
    recipe.servings = data.get("servings")
    recipe.total_time = data.get("total_time")
    recipe.rating = data.get("rating")
    recipe.rating_count = data.get("rating_count")
    recipe.category = data.get("category")
    recipe.raw_json = json.dumps(data, ensure_ascii=False)
    recipe.original_title = data.get("original_title")
    recipe.original_instructions = data.get("original_instructions")

    # přepiš ingredience
    recipe.ingredients.clear()
    db.flush()

    lines = data.get("ingredients", [])
    original_lines = data.get("original_ingredients")  # stejná délka/pořadí jako lines
    with t.phase("normalize"):
        normalized = normalize_lines(db, lines)
    for i, norm in enumerate(normalized):
        ing = norm["ingredient"]
        grams = grams_for(norm["amount"], norm["unit"], ing)
        ri = RecipeIngredient(
            raw_text=norm["raw_text"][:400],
            original_raw_text=(original_lines[i][:400] if original_lines else None),
            ingredient_id=ing.id if ing else None,
            amount=norm["amount"],
            unit=norm["unit"],
            grams=grams,
            kcal=kcal_for(grams, ing),
        )
        recipe.ingredients.append(ri)

    recompute_recipe_kcal(recipe)
    with t.phase("commit"):
        db.commit()
        db.refresh(recipe)
    t.dump(f"({len(lines)} surovin)")
    return recipe


def persist(db: Session, data: dict) -> Recipe:
    """Veřejný alias pro uložení už připraveného receptu (URL scrape i foto-import)."""
    return _persist(db, data)
