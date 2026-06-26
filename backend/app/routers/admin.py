"""Administrace: runtime nastavení nástrojů, RECIPE_DOMAINS, import
NutriDatabaze a kompletní export/import dat (DB-agnostické přes JSON)."""
from __future__ import annotations

import base64
import io
import json
import logging
import tempfile
import threading
import time
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Date, DateTime, delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import (
    AppSetting,
    Ingredient,
    IngredientAlias,
    PantryItem,
    Recipe,
    RecipeEmbedding,
    RecipeIngredient,
    ShoppingItem,
)

log = logging.getLogger("kucharka.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])

# tabulky pro zálohu – pořadí kvůli cizím klíčům
EXPORT_ORDER = [
    ("ingredient", Ingredient),
    ("ingredient_alias", IngredientAlias),
    ("recipe", Recipe),
    ("recipe_ingredient", RecipeIngredient),
    ("recipe_embedding", RecipeEmbedding),
    ("pantry_item", PantryItem),
    ("shopping_item", ShoppingItem),
]
_BINARY_COLS = {"vec"}


# ----------------------------- nastavení -----------------------------
@router.get("/test-ollama")
def test_ollama():
    import httpx

    url = settings.ollama_url
    if not url:
        return {"reachable": False, "error": "OLLAMA_URL není nastaveno."}
    try:
        r = httpx.get(f"{url}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "url": url, "error": str(exc)}

    def has(name: str) -> bool:
        base = name.split(":")[0]
        return any(m == name or m.split(":")[0] == base for m in models)

    return {
        "reachable": True,
        "url": url,
        "models": models,
        "chat_model": settings.ollama_model,
        "has_chat_model": has(settings.ollama_model),
        "embed_model": settings.embed_model,
        "has_embed_model": has(settings.embed_model),
    }


class SettingsUpdate(BaseModel):
    values: dict


@router.get("/settings")
def get_settings():
    return settings.as_admin()


@router.put("/settings")
def put_settings(req: SettingsUpdate, db: Session = Depends(get_db)):
    applied = {}
    crawler_changed = False
    service_changed = False
    for key, value in req.values.items():
        if key not in settings.ADMIN_KEYS:
            continue
        settings.set_admin(key, value)
        if key in settings.CRAWLER_KEYS:
            crawler_changed = True
        if key in settings.SERVICE_KEYS:
            service_changed = True
        sval = value if isinstance(value, str) else json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=sval))
        else:
            row.value = sval
        applied[key] = True
    db.commit()
    if crawler_changed or service_changed:
        from .. import scheduler

        if crawler_changed:
            scheduler.configure_crawler()
        if service_changed:
            scheduler.configure_translate()
            scheduler.configure_match()
            scheduler.configure_enrichment()
            scheduler.configure_images()
            scheduler.configure_llm_match()
    return {"applied": list(applied), "settings": settings.as_admin()}


class PasswordUpdate(BaseModel):
    password: str  # prázdné = zrušit zabezpečení


@router.put("/password")
def set_password(req: PasswordUpdate):
    from .. import auth

    auth.set_password(req.password.strip() or None)
    return {"auth_enabled": settings.auth_enabled}


# ----------------------------- RECIPE_DOMAINS -----------------------------
@router.get("/recipe-domains/export")
def export_domains():
    text = "\n".join(sorted(settings.recipe_domains))
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=recipe-domains.txt"},
    )


@router.post("/recipe-domains/import")
async def import_domains(file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = (await file.read()).decode("utf-8", "ignore")
    settings.set_admin("recipe_domains", raw)
    csv = ",".join(sorted(settings.recipe_domains))
    row = db.get(AppSetting, "recipe_domains")
    if row is None:
        db.add(AppSetting(key="recipe_domains", value=csv))
    else:
        row.value = csv
    db.commit()
    return {"count": len(settings.recipe_domains), "domains": sorted(settings.recipe_domains)}


# ----------------------------- NutriDatabaze -----------------------------
_nutri_state: dict = {"running": False, "message": "", "inserted": 0, "enriched": 0,
                      "merged": 0, "recomputed": 0, "finished_at": None}


@router.get("/nutridb/status")
def nutridb_status():
    return dict(_nutri_state)


def _run_nutridb(path: str, merge: bool):
    from ..seed.import_nutridb import import_csv, merge_ollama, recompute_all

    db = SessionLocal()
    try:
        _nutri_state.update(running=True, message="importuji CSV…", finished_at=None)
        ins, enr = import_csv(db, path)
        _nutri_state.update(inserted=ins, enriched=enr)
        if merge:
            _nutri_state.update(message="slučuji ollama-duplikáty…")
            _nutri_state.update(merged=merge_ollama(db))
        _nutri_state.update(message="přepočítávám kalorie…")
        _nutri_state.update(recomputed=recompute_all(db))
        _nutri_state.update(message="hotovo")
        log.info("NutriDatabaze import: %s nových, %s zpřesněných", ins, enr)
    except Exception as exc:  # noqa: BLE001
        _nutri_state.update(message=f"chyba: {exc}")
        log.warning("NutriDatabaze import selhal: %s", exc)
    finally:
        _nutri_state.update(running=False, finished_at=time.time())
        db.close()


@router.post("/nutridb/import")
async def nutridb_import(file: UploadFile = File(...), merge: bool = Form(True)):
    if _nutri_state["running"]:
        return {"started": False, "reason": "Import už běží."}
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    tmp.write(data)
    tmp.close()
    _nutri_state.update(inserted=0, enriched=0, merged=0, recomputed=0, message="")
    threading.Thread(target=_run_nutridb, args=(tmp.name, merge), daemon=True).start()
    return {"started": True}


@router.get("/ingredients/export")
def export_ingredients(db: Session = Depends(get_db)):
    cols = ["code", "name_cs", "name_en", "kcal_100g", "protein_100g",
            "carbs_100g", "fat_100g", "fiber_100g", "source"]
    out = io.StringIO()
    import csv as _csv

    w = _csv.writer(out, delimiter=";")
    w.writerow(cols)
    for ing in db.scalars(select(Ingredient).order_by(Ingredient.name_cs)):
        w.writerow([getattr(ing, c) if getattr(ing, c) is not None else "" for c in cols])
    return StreamingResponse(
        io.BytesIO(out.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ingredients.csv"},
    )


# ----------------------------- záloha DB -----------------------------
def _row_to_dict(obj, model) -> dict:
    d = {}
    for col in model.__table__.columns:
        val = getattr(obj, col.name)
        if col.name in _BINARY_COLS and isinstance(val, (bytes, bytearray)):
            val = base64.b64encode(val).decode("ascii")
        elif hasattr(val, "isoformat"):
            val = val.isoformat()
        d[col.name] = val
    return d


@router.get("/db/export")
def export_db(db: Session = Depends(get_db)):
    dump = {"version": 1, "tables": {}}
    for name, model in EXPORT_ORDER:
        dump["tables"][name] = [
            _row_to_dict(o, model) for o in db.scalars(select(model)).all()
        ]
    payload = json.dumps(dump, ensure_ascii=False).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=kucharka-backup.json"},
    )


@router.post("/db/import")
async def import_db(file: UploadFile = File(...), mode: str = Form("replace")):
    raw = await file.read()
    try:
        dump = json.loads(raw.decode("utf-8"))
        tables = dump["tables"]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Neplatný soubor: {exc}"}

    db = SessionLocal()
    try:
        if mode == "replace":
            for name, model in reversed(EXPORT_ORDER):
                db.execute(delete(model))
            db.commit()
        counts = {}
        for name, model in EXPORT_ORDER:
            rows = tables.get(name, [])
            dt_cols = {
                c.name: c.type for c in model.__table__.columns
                if isinstance(c.type, (DateTime, Date))
            }
            for r in rows:
                data = dict(r)
                for col in _BINARY_COLS:
                    if col in data and isinstance(data[col], str):
                        data[col] = base64.b64decode(data[col])
                for col, ctype in dt_cols.items():
                    if isinstance(data.get(col), str) and data[col]:
                        data[col] = (
                            datetime.fromisoformat(data[col])
                            if isinstance(ctype, DateTime)
                            else date.fromisoformat(data[col])
                        )
                db.merge(model(**data))
            counts[name] = len(rows)
            db.commit()
        return {"ok": True, "mode": mode, "counts": counts}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


@router.get("/crawl-sources")
def list_crawl_sources(db: Session = Depends(get_db)):
    """Stav cache sitemap per doména. Užitečné pro diagnostiku inkrementálního crawleru."""
    from ..models import CrawlSource
    rows = db.scalars(select(CrawlSource).order_by(CrawlSource.domain)).all()
    return [
        {
            "domain": r.domain,
            "sitemap_url": r.sitemap_url,
            "etag": r.etag,
            "http_last_modified": r.http_last_modified,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "last_lastmod": r.last_lastmod.isoformat() if r.last_lastmod else None,
            "total_seen": r.total_seen,
            "total_ingested": r.total_ingested,
            "last_error": r.last_error,
        }
        for r in rows
    ]


@router.post("/crawl-sources/reset")
def reset_crawl_sources(
    domain: str | None = None,
    db: Session = Depends(get_db),
):
    """Vynuluj inkrementální cache. Bez parametru → všechny domény; s `domain=foo.cz` jen jedna.

    Použij když chceš vynutit kompletní re-discovery (např. po úpravě webových
    sitemap nebo když si myslíš, že crawler propásl recepty).
    """
    from ..models import CrawlSource
    if domain:
        cs = db.get(CrawlSource, domain)
        if cs is None:
            raise HTTPException(404, f"crawl_source {domain} neexistuje")
        db.delete(cs)
        db.commit()
        return {"reset": [domain]}
    rows = db.scalars(select(CrawlSource)).all()
    domains = [r.domain for r in rows]
    for r in rows:
        db.delete(r)
    db.commit()
    return {"reset": domains}
