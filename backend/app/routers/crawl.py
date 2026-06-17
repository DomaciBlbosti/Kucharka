"""API pro autonomní crawler."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..modules import crawler

router = APIRouter(prefix="/api/crawl", tags=["crawler"])


class CrawlRequest(BaseModel):
    queries: list[str] | None = None
    max_recipes: int = 30
    per_query: int = 8


@router.get("/status")
def crawl_status():
    s = crawler.status()
    s["scheduler_enabled"] = settings.crawler_enabled
    s["auto_ingredients"] = settings.auto_ingredients
    return s


@router.post("/run")
def crawl_run(req: CrawlRequest):
    if not settings.searxng_enabled:
        return {"started": False, "reason": "SearXNG není nastavený (SEARXNG_URL)."}
    started = crawler.crawl_async(
        queries=req.queries,
        max_recipes=req.max_recipes,
        per_query=req.per_query,
    )
    return {"started": started, "status": crawler.status()}
