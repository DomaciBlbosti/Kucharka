"""Klient jádra k webové appce (kuchařce) přes ingest kontrakt."""
from __future__ import annotations

import httpx

from . import config


def _base() -> str:
    return config.get()["web_api_url"].rstrip("/")


def _headers() -> dict:
    tok = config.get()["core_token"]
    return {"X-Core-Token": tok} if tok else {}


def ping() -> dict:
    r = httpx.get(f"{_base()}/api/ingest/ping", headers=_headers(), timeout=8)
    r.raise_for_status()
    return r.json()


def dictionary() -> list[dict]:
    r = httpx.get(f"{_base()}/api/ingest/dictionary", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["ingredients"]


def filter_new(urls: list[str]) -> list[str]:
    r = httpx.post(f"{_base()}/api/ingest/filter-new", headers=_headers(), json=urls, timeout=30)
    r.raise_for_status()
    return r.json()["new"]


def upsert_recipe(payload: dict) -> dict:
    r = httpx.post(f"{_base()}/api/ingest/recipe", headers=_headers(), json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def recipes_needing(need: str, limit: int = 50) -> list[dict]:
    r = httpx.get(f"{_base()}/api/ingest/recipes", headers=_headers(),
                  params={"need": need, "limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json()["items"]


def patch_recipe(recipe_id: int, payload: dict) -> None:
    r = httpx.patch(f"{_base()}/api/ingest/recipe/{recipe_id}", headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()


def ingredients_needing(need: str, limit: int = 200) -> list[dict]:
    r = httpx.get(f"{_base()}/api/ingest/ingredients", headers=_headers(),
                  params={"need": need, "limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json()["items"]


def patch_ingredient(ingredient_id: int, payload: dict) -> None:
    r = httpx.patch(f"{_base()}/api/ingest/ingredient/{ingredient_id}", headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
