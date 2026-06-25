"""Ingest pipeline (fast-path): URL → recept v DB.

Žádný překlad, žádný LLM. Cíl je co nejrychleji uložit syrová metadata
a syrové řádky ingrediencí. Vše ostatní (matching surovin, výpočet kcal,
stažení obrázku, případný překlad) řeší worker přes statusové sloupce
v tabulce `recipe`.

Statusy po ingestu:
    crawl_status      = 'scraped'
    enrichment_status = 'pending'    (nebo 'done' u zachovaných existujících
                                       dat při re-crawlu beze změny ingrediencí)
    image_status      = 'pending'    (pokud image_url existuje)
                      = 'none'       (pokud neexistuje)

Idempotence podle source_url:
- Nový recept → upsert se statusem 'pending'.
- Existující recept (re-crawl) → aktualizuj metadata; pokud se ingredience
  změnily, vyresetuj enrichment_status na 'pending'. Pokud se image_url
  změnil, smaž lokální soubory a status zpět na 'pending'.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Recipe, RecipeIngredient
from . import scraper


def ingest_url(db: Session, url: str) -> Recipe | None:
    """Stáhne, extrahuje a uloží recept. Vrátí Recipe nebo None při selhání."""
    data = scraper.fetch_and_extract(url)
    if data is None:
        return None
    return _persist(db, data)


def _persist(db: Session, data: dict) -> Recipe:
    recipe = db.scalar(select(Recipe).where(Recipe.source_url == data["source_url"]))
    is_new = recipe is None
    if is_new:
        recipe = Recipe(source_url=data["source_url"])
        db.add(recipe)

    old_image_url = recipe.image_url if not is_new else None

    # Metadata
    recipe.title = data["title"]
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
    recipe.crawl_status = "scraped"

    # Ingredience jako raw_text, žádné parsování — to dělá enrichment worker.
    new_lines = [str(s) for s in data.get("ingredients", []) if s]
    old_lines = [ri.raw_text for ri in (recipe.ingredients or [])]
    ingredients_changed = is_new or new_lines != old_lines

    if ingredients_changed:
        recipe.ingredients.clear()
        db.flush()
        for line in new_lines:
            recipe.ingredients.append(RecipeIngredient(raw_text=line[:400]))
        # Reset enrichmentu — staré matche nejsou platné pro nové řádky.
        recipe.enrichment_status = "pending"
        recipe.enrichment_attempts = 0
        recipe.enrichment_error = None
        recipe.last_enriched_at = None
        recipe.kcal_per_serving = None

    # Obrázek — pokud se URL změnila, zahoď lokální soubor.
    image_url_changed = (old_image_url or "") != (recipe.image_url or "")
    if is_new or image_url_changed:
        if recipe.image_url:
            recipe.image_status = "pending"
        else:
            recipe.image_status = "none"
        recipe.local_image_path = None
        recipe.local_thumb_path = None

    db.commit()
    db.refresh(recipe)
    return recipe
