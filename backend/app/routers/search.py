"""API pro získávání receptů – ruční URL i discovery přes SearXNG."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..modules import discovery
from ..modules.ingest import ingest_url
from ..schemas import IngestRequest, RecipeDetail, SearchRequest

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/status")
def status():
    return {
        "searxng": settings.searxng_enabled,
        "ollama": settings.ollama_enabled,
    }


@router.get("/ollama")
def ollama_status():
    """Diagnostika připojení k Ollamě a dostupnosti modelu."""
    from ..modules.normalizer import ollama_check

    return ollama_check()


@router.post("/discover")
def discover(req: SearchRequest):
    """Vrať kandidátní URL z SearXNG (bez stahování)."""
    return {"results": discovery.search(req.query)}


@router.post("/ingest", response_model=RecipeDetail | None)
def ingest(req: IngestRequest, db: Session = Depends(get_db)):
    """Stáhni, vyparsuj a ulož recept z dané URL."""
    recipe = ingest_url(db, req.url)
    if recipe is None:
        return None
    # znovu načti přes detailní endpoint logiku
    from .recipes import get_recipe

    return get_recipe(recipe.id, db)
