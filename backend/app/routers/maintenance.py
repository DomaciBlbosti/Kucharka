"""Údržba dat: dopárování nenapárovaných surovin u receptů."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..modules import backfill

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


class BackfillRequest(BaseModel):
    create_missing: bool = True  # smí LLM vytvářet nové suroviny


@router.get("/match-status")
def match_status():
    s = backfill.status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/backfill")
def run_backfill(req: BackfillRequest):
    create = req.create_missing and settings.ollama_enabled
    started = backfill.backfill_async(create_missing=create)
    return {"started": started, "status": backfill.status()}
