"""RAG generování receptů: indexace embeddingů + vymýšlení nových receptů."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..modules import rag

router = APIRouter(prefix="/api/generate", tags=["generate"])


class IndexRequest(BaseModel):
    rebuild: bool = False


class GenerateRequest(BaseModel):
    prompt: str
    k: int | None = None
    max_kcal: float | None = None
    min_rating: float | None = None


class SaveRequest(BaseModel):
    recipe: dict


@router.get("/status")
def status():
    s = rag.index_status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/index")
def index(req: IndexRequest):
    if not settings.ollama_enabled:
        return {"started": False, "reason": "Ollama není nastavená (OLLAMA_URL)."}
    started = rag.index_async(rebuild=req.rebuild)
    return {"started": started, "status": rag.index_status()}


@router.post("")
def generate(req: GenerateRequest, db: Session = Depends(get_db)):
    if not settings.ollama_enabled:
        raise HTTPException(400, "Ollama není nastavená (OLLAMA_URL).")
    if not req.prompt.strip():
        raise HTTPException(400, "Prázdné zadání.")
    try:
        return rag.generate(
            db, req.prompt.strip(), k=req.k,
            max_kcal=req.max_kcal, min_rating=req.min_rating,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Generování selhalo: {exc}") from exc


@router.post("/save")
def save(req: SaveRequest, db: Session = Depends(get_db)):
    recipe = rag.save_generated(db, req.recipe)
    return {"id": recipe.id, "title": recipe.title}
