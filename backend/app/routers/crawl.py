"""API pro autonomní crawler."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..modules import crawler

router = APIRouter(prefix="/api/crawl", tags=["crawler"])


class CrawlRequest(BaseModel):
    mode: str = "sites"  # "sites" = procházet weby (default), "query" = přes SearXNG
    domains: list[str] | None = None
    queries: list[str] | None = None
    max_recipes: int = 30
    per_site: int = 12
    per_query: int = 8


@router.get("/status")
def crawl_status():
    s = crawler.status()
    s["scheduler_enabled"] = settings.crawler_enabled
    s["auto_ingredients"] = settings.auto_ingredients
    s["domains_count"] = len(settings.recipe_domains)
    return s


@router.post("/run")
def crawl_run(req: CrawlRequest):
    if req.mode == "query":
        if not settings.searxng_enabled:
            return {"started": False, "reason": "SearXNG není nastavený (SEARXNG_URL)."}
        started = crawler.crawl_async(
            queries=req.queries,
            max_recipes=req.max_recipes,
            per_query=req.per_query,
        )
    else:  # sites – procházení webů přes sitemapy (nepotřebuje SearXNG)
        started = crawler.crawl_sites_async(
            domains=req.domains,
            max_recipes=req.max_recipes,
            per_site=req.per_site,
        )
    return {"started": started, "status": crawler.status()}
