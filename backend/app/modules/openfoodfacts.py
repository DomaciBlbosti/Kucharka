"""Vyhledání produktu podle čárového kódu přes Open Food Facts (zdarma, bez klíče)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("kucharka.openfoodfacts")

_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
_FIELDS = "product_name,product_name_cs,generic_name,brands,image_front_small_url"


def lookup(barcode: str) -> dict | None:
    """Vrať {'name','brand','image_url'} nebo None, když produkt neexistuje/API selhalo."""
    try:
        r = httpx.get(
            _URL.format(code=barcode),
            params={"fields": _FIELDS},
            headers={"User-Agent": "Kucharka/1.0 (domaci self-hosted app)"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("OFF lookup %s selhal: %s", barcode, exc)
        return None
    if data.get("status") != 1:
        return None
    p = data.get("product", {})
    name = (p.get("product_name_cs") or p.get("product_name") or p.get("generic_name") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "brand": (p.get("brands") or "").split(",")[0].strip() or None,
        "image_url": p.get("image_front_small_url"),
    }
