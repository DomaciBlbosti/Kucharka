"""Stažení a extrakce receptu z URL.

Stahování řešíme sami (httpx) – recipe-scrapers jen parsuje HTML. Per-doménový
throttle drží slušné tempo. Extrakce přes scrape_html(wild_mode=True), takže
funguje i na webech bez dedikovaného scraperu, pokud mají schema.org/Recipe.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from ..config import settings
from .normalizer import is_section_header

try:
    from recipe_scrapers import scrape_html
except Exception:  # pragma: no cover - knihovna nemusí být při testu
    scrape_html = None  # type: ignore

_last_hit: dict[str, float] = {}


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def _throttle(domain: str) -> None:
    now = time.monotonic()
    last = _last_hit.get(domain, 0.0)
    wait = settings.scrape_delay - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_hit[domain] = time.monotonic()


def fetch_html(url: str) -> str:
    _throttle(domain_of(url))
    headers = {"User-Agent": settings.user_agent, "Accept-Language": "cs,en;q=0.8"}
    with httpx.Client(
        follow_redirects=True,
        timeout=settings.http_timeout,
        headers=headers,
        verify=settings.scraper_verify,
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def _safe(fn, default=None):
    try:
        val = fn()
        return val if val not in ("", []) else default
    except Exception:
        return default


def extract(html: str, url: str) -> dict | None:
    """Vrať strukturovaný recept, nebo None když se nepodařilo rozumně vyparsovat."""
    if scrape_html is None:
        return None
    try:
        s = scrape_html(html, org_url=url, wild_mode=True)
    except TypeError:
        # starší/jiná signatura
        s = scrape_html(html, url)
    except Exception:
        return None

    ingredients = _safe(s.ingredients, []) or []
    # Weby často vkládají do seznamu ingrediencí i nadpisy skupin ("Marináda:",
    # "Na ozdobu:") jako další položku – to není surovina, jen by to zbytečně
    # zaplevelilo ruční párování. Vyhodíme je hned tady, než se vůbec uloží.
    ingredients = [i for i in ingredients if not is_section_header(i)]
    title = _safe(s.title)
    if not title or len(ingredients) < 2:
        return None  # pravděpodobně špatný parse / listing stránka

    return {
        "title": title,
        "source_url": url,
        "source_domain": domain_of(url),
        "image_url": _safe(s.image),
        "instructions": _safe(s.instructions),
        "ingredients": ingredients,
        "servings": _to_int(_safe(s.yields)),
        "total_time": _safe(s.total_time),
        "rating": _safe(getattr(s, "ratings", lambda: None)),
        "rating_count": _safe(getattr(s, "ratings_count", lambda: None)),
        "category": _safe(getattr(s, "category", lambda: None)),
    }


def fetch_and_extract(url: str) -> dict | None:
    return extract(fetch_html(url), url)


def _to_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    import re

    m = re.search(r"\d+", str(val))
    return int(m.group()) if m else None
