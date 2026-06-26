"""API pro práci s tagy.

GET    /api/tags                        – seznam všech tagů (pro UI menu)
GET    /api/tags/counts                 – počty receptů per tag (pro skrytí prázdných)
POST   /api/recipes/{id}/tags           – ručně přidat tag (source='manual')
DELETE /api/recipes/{id}/tags/{tag_id}  – odebrat tag (jakýkoliv source)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Recipe, RecipeTag, Tag
from ..schemas import TagOut

router = APIRouter(prefix="/api", tags=["tags"])


@router.get("/tags", response_model=list[TagOut])
def list_tags(db: Session = Depends(get_db)):
    """Vrátí všechny existující tagy. Pořadí: namespace, slug."""
    tags = db.scalars(select(Tag).order_by(Tag.namespace, Tag.slug)).all()
    return tags


@router.get("/tags/counts")
def tag_counts(db: Session = Depends(get_db)):
    """Počty receptů na tag. Užitečné pro UI: skrýt taggy bez obsahu."""
    rows = db.execute(
        select(
            Tag.namespace,
            Tag.slug,
            Tag.label_cs,
            func.count(RecipeTag.recipe_id).label("n"),
        )
        .outerjoin(RecipeTag, Tag.id == RecipeTag.tag_id)
        .group_by(Tag.id)
        .order_by(Tag.namespace, Tag.slug)
    ).all()
    return [
        {"namespace": ns, "slug": slug, "label_cs": label, "count": n}
        for ns, slug, label, n in rows
    ]


class TagAdd(BaseModel):
    namespace: str
    slug: str


@router.post("/recipes/{recipe_id}/tags", status_code=201)
def add_tag(recipe_id: int, body: TagAdd, db: Session = Depends(get_db)):
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise HTTPException(404, "Recept neexistuje")
    tag = db.scalar(
        select(Tag).where(Tag.namespace == body.namespace, Tag.slug == body.slug)
    )
    if tag is None:
        raise HTTPException(404, f"Tag {body.namespace}:{body.slug} neexistuje")
    # Pokud už tam je (jako auto), přepiš na manual; jinak vytvoř.
    existing = db.get(RecipeTag, (recipe.id, tag.id))
    if existing is not None:
        existing.source = "manual"
    else:
        db.add(RecipeTag(recipe_id=recipe.id, tag_id=tag.id, source="manual"))
    db.commit()
    return {"recipe_id": recipe.id, "tag_id": tag.id, "source": "manual"}


@router.delete("/recipes/{recipe_id}/tags/{tag_id}", status_code=204)
def remove_tag(recipe_id: int, tag_id: int, db: Session = Depends(get_db)):
    rt = db.get(RecipeTag, (recipe_id, tag_id))
    if rt is None:
        raise HTTPException(404, "Tag na receptu není")
    db.delete(rt)
    db.commit()
