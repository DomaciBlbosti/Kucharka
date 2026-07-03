"""Ukládání souborů nahraných uživatelem (fotky receptů apod.).

Ukládá se MIMO /app volume (ten při self-update prochází git pull/clone,
takže by nahrané soubory mohl ztratit) – do samostatné cesty settings.upload_dir,
kterou appka servíruje na /uploads/... (viz main.py StaticFiles mount).
"""
from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from ..config import settings

log = logging.getLogger("kucharka.uploads")

_MAX_WIDTH = 1600


def save_recipe_photo(image_bytes: bytes) -> str | None:
    """Ulož fotku (zmenšenou) do upload_dir/recipe-photos a vrať veřejnou URL
    cestu (/uploads/recipe-photos/<jméno>.jpg), nebo None při chybě."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.width > _MAX_WIDTH:
            ratio = _MAX_WIDTH / img.width
            img = img.resize((_MAX_WIDTH, int(img.height * ratio)))

        folder = Path(settings.upload_dir) / "recipe-photos"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.jpg"
        img.save(folder / name, format="JPEG", quality=88)
        return f"/uploads/recipe-photos/{name}"
    except Exception as exc:  # noqa: BLE001
        log.warning("uložení fotky receptu selhalo: %s", exc)
        return None
