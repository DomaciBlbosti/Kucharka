"""Stažení a extrakce receptu z URL (kopie webové verze, bez DB)."""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx

from . import config

try:
    from recipe_scrapers import scrape_html
except Exception:  # pragma: no cover
    scrape_html = None  # type: ignore

_UA = "Mozilla/5.0 (compatible; KucharkaCore/1.0)"
_SCRAPE_DELAY = 1.5
_HTTP_TIMEOUT = 20.0
_last_hit: dict[str, float] = {}


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def _verify():
    c = config.get()
    if not c["scraper_verify_ssl"]:
        return False
    bundle = "/etc/ssl/certs/ca-certificates.crt"
    import os
    return bundle if os.path.exists(bundle) else True


def _throttle(domain: str) -> None:
    now = time.monotonic()
    wait = _SCRAPE_DELAY - (now - _last_hit.get(domain, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[domain] = time.monotonic()


def fetch_html(url: str) -> str:
    _throttle(domain_of(url))
    headers = {"User-Agent": _UA, "Accept-Language": "cs,en;q=0.8"}
    with httpx.Client(follow_redirects=True, timeout=_HTTP_TIMEOUT, headers=headers, verify=_verify()) as client:
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
    if scrape_html is None:
        return None
    try:
        s = scrape_html(html, org_url=url, wild_mode=True)
    except TypeError:
        s = scrape_html(html, url)
    except Exception:
        return None
    ingredients = _safe(s.ingredients, []) or []
    title = _safe(s.title)
    if not title or len(ingredients) < 2:
        return None
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
    }


def fetch_and_extract(url: str) -> dict | None:
    return extract(fetch_html(url), url)


def _to_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    m = re.search(r"\d+", str(val))
    return int(m.group()) if m else None
