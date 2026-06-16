"""Discovery – vyhledání kandidátních URL receptů přes SearXNG.

Volitelné: bez nastavené SEARXNG_URL prostě vrací prázdný seznam a appka jede
na ruční vkládání URL.
"""
from __future__ import annotations

import httpx

from ..config import settings
from .scraper import domain_of


def search(query: str, limit: int = 12) -> list[dict]:
    if not settings.searxng_enabled:
        return []
    params = {
        "q": f"{query} recept",
        "format": "json",
        "language": "cs",
        "categories": "general",
    }
    headers = {"User-Agent": settings.user_agent}
    try:
        with httpx.Client(timeout=settings.http_timeout, headers=headers) as client:
            r = client.get(f"{settings.searxng_url}/search", params=params)
            r.raise_for_status()
            results = r.json().get("results", [])
    except Exception:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for item in results:
        url = item.get("url", "")
        if not url or url in seen:
            continue
        dom = domain_of(url)
        if settings.recipe_domains and dom not in settings.recipe_domains:
            continue
        seen.add(url)
        out.append({"url": url, "title": item.get("title", ""), "domain": dom})
        if len(out) >= limit:
            break
    return out
