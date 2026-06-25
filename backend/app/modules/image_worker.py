"""Image worker.

Stahuje obrázky receptů z `recipe.image_url` na lokální disk a vytváří
resize (thumb 400px, full 1200px). Aktualizuje `image_status`,
`local_image_path`, `local_thumb_path`.

Cílový adresář: `settings.images_dir` (default `/data/images`).
Pojmenování: `{recipe_id}_full.{ext}` a `{recipe_id}_thumb.{ext}`.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe

log = logging.getLogger("kucharka.image_worker")

THUMB_SIZE = (400, 400)
FULL_SIZE = (1200, 1200)
JPEG_QUALITY = 85
MAX_BYTES = 25 * 1024 * 1024  # 25 MB — větší obrázek je s vysokou pravděpodobností nesmysl

# Akceptované content typy
_OK_MIMES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _images_dir() -> Path:
    p = Path(getattr(settings, "images_dir", "/data/images"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ext_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "webp", "gif"}:
        return "jpg" if suffix == "jpeg" else suffix
    return "jpg"


def _download(url: str, timeout: float = 20.0) -> bytes:
    headers = {
        "User-Agent": getattr(settings, "user_agent", "Kucharka/1.0"),
        "Accept": "image/*",
    }
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as cl:
        r = cl.get(url)
        r.raise_for_status()
        if "content-type" in r.headers:
            mime = r.headers["content-type"].split(";", 1)[0].strip().lower()
            if mime not in _OK_MIMES:
                raise ValueError(f"nepodporovaný content-type: {mime}")
        if len(r.content) > MAX_BYTES:
            raise ValueError(f"obrázek je příliš velký: {len(r.content)} B")
        return r.content


def _save_variants(recipe_id: int, raw: bytes, ext: str) -> tuple[str, str]:
    """Uloží full a thumb. Vrátí (full_path, thumb_path) relativní k images_dir."""
    out_dir = _images_dir()
    img = Image.open(io.BytesIO(raw))
    # GIF a P-mode → konverze do RGB (jinak JPEG save spadne)
    if img.mode in ("P", "LA", "RGBA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode in ("LA", "RGBA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Full
    full_img = img.copy()
    full_img.thumbnail(FULL_SIZE, Image.LANCZOS)
    full_name = f"{recipe_id}_full.jpg"
    full_path = out_dir / full_name
    full_img.save(full_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

    # Thumb
    thumb_img = img.copy()
    thumb_img.thumbnail(THUMB_SIZE, Image.LANCZOS)
    thumb_name = f"{recipe_id}_thumb.jpg"
    thumb_path = out_dir / thumb_name
    thumb_img.save(thumb_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

    return full_name, thumb_name


def process_one(recipe: Recipe) -> bool:
    """Stáhne a zpracuje obrázek pro jeden recept. Vrátí True při úspěchu."""
    if not recipe.image_url:
        recipe.image_status = "none"
        return False
    try:
        raw = _download(recipe.image_url)
        ext = _ext_from_url(recipe.image_url)
        full_name, thumb_name = _save_variants(recipe.id, raw, ext)
        recipe.local_image_path = full_name
        recipe.local_thumb_path = thumb_name
        recipe.image_status = "downloaded"
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Image stahování %s selhalo: %s", recipe.image_url, exc)
        recipe.image_status = "failed"
        return False


def process_batch(batch_size: int | None = None) -> dict:
    """Worker entrypoint."""
    batch_size = batch_size or getattr(settings, "image_batch_size", 10)
    db = SessionLocal()
    try:
        recipes = db.scalars(
            select(Recipe)
            .where(Recipe.image_status == "pending")
            .order_by(Recipe.id)
            .limit(batch_size)
        ).all()
        if not recipes:
            return {"recipes": 0}
        ok = 0
        fail = 0
        for r in recipes:
            if process_one(r):
                ok += 1
            else:
                fail += 1
            db.commit()
        result = {"recipes": len(recipes), "downloaded": ok, "failed": fail}
        log.info("Image batch: %s", result)
        return result
    finally:
        db.close()


def status() -> dict:
    db = SessionLocal()
    try:
        from sqlalchemy import func
        rows = db.execute(
            select(Recipe.image_status, func.count(Recipe.id))
            .group_by(Recipe.image_status)
        ).all()
        return {status: count for status, count in rows}
    finally:
        db.close()
