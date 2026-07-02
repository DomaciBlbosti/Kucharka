"""Objevování receptových URL přes sitemapy (bez DB)."""
from __future__ import annotations

import gzip
import random
import re

import httpx

from . import config

_RECIPE_HINT = re.compile(r"/(recept|recipe|recepty|recipes)/", re.I)
_EXCLUDE = re.compile(
    r"/(clanky|clanek|magazin|magazine|tag|tags|stitek|kategorie|category|"
    r"categories|menu|user|users|author|autor|blog|temata|tema|hledat|search)/",
    re.I,
)
_UA = "Mozilla/5.0 (compatible; KucharkaCore/1.0)"


def _verify():
    if not config.get()["scraper_verify_ssl"]:
        return False
    import os
    bundle = "/etc/ssl/certs/ca-certificates.crt"
    return bundle if os.path.exists(bundle) else True


def _fetch_bytes(url: str) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=20.0,
                      headers={"User-Agent": _UA}, verify=_verify()) as cl:
        r = cl.get(url)
        r.raise_for_status()
        return r.content


def _maybe_gunzip(content: bytes) -> bytes:
    if content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except Exception:  # noqa: BLE001
            return content
    return content


def _sitemaps_for(domain: str) -> list[str]:
    out: list[str] = []
    try:
        robots = _fetch_bytes(f"https://{domain}/robots.txt").decode("utf-8", "ignore")
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                out.append(line.split(":", 1)[1].strip())
    except Exception:  # noqa: BLE001
        pass
    return out or [f"https://{domain}/sitemap.xml"]


def _urls_from_sitemap(url: str, depth: int = 0) -> list[str]:
    if depth > 2:
        return []
    try:
        text = _maybe_gunzip(_fetch_bytes(url)).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)", text)
    if "<sitemapindex" in text.lower():
        random.shuffle(locs)
        out: list[str] = []
        for sm in locs[:8]:
            out += _urls_from_sitemap(sm, depth + 1)
            if len(out) > 3000:
                break
        return out
    return locs


def discover_site(domain: str, max_urls: int = 120) -> list[str]:
    all_urls: list[str] = []
    for sm in _sitemaps_for(domain)[:5]:
        all_urls += _urls_from_sitemap(sm)
        if len(all_urls) >= max_urls * 6:
            break
    recipe_urls = [u for u in all_urls if _RECIPE_HINT.search(u) and not _EXCLUDE.search(u)]
    if not recipe_urls:
        recipe_urls = [u for u in all_urls if not _EXCLUDE.search(u)]
    random.shuffle(recipe_urls)
    return recipe_urls[:max_urls]


def domains() -> list[str]:
    return [d.strip().lower() for d in config.get()["recipe_domains"].split(",") if d.strip()]
