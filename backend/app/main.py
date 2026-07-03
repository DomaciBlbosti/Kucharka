"""Hlavní FastAPI aplikace.

Servíruje API pod /api/* a (pokud existuje) sestavený frontend ze static/.
Při startu vytvoří tabulky a nasype základní suroviny, pokud je DB prázdná.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import (
    admin, auth as auth_router, crawl, generate, ingredients, maintenance,
    barcode, hmi, hmi_page, ingest, lidl, mealplan, pantry, receipt, recipes, search, system,
)
from .seed.starter_ingredients import seed_starter
from .seed.starter_tags import seed_tags

log = logging.getLogger("kucharka")
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def init_db(retries: int = 10) -> None:
    """Počkej na DB (MariaDB při startu kontejneru naběhne se zpožděním)."""
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(engine)
            _ensure_columns()
            break
        except OperationalError as exc:
            log.warning("DB zatím nedostupná (%s/%s): %s", attempt, retries, exc)
            time.sleep(3)
    else:
        log.error("Nepodařilo se připojit k DB.")
        return
    db = SessionLocal()
    try:
        n = seed_starter(db)
        if n:
            log.info("Naseedováno %s základních surovin.", n)
        nt = seed_tags(db)
        if nt:
            log.info("Naseedováno %s kanonických tagů.", nt)
        _load_settings_overrides(db)
    finally:
        db.close()


def _ensure_columns() -> None:
    """Doplň chybějící sloupce do existujících tabulek (lehká migrace)."""
    from sqlalchemy import inspect, text

    wanted = {
        "ingredient": [("category_path", "VARCHAR(200)")],
        "recipe": [
            ("user_rating", "INTEGER"),
            ("user_note", "TEXT"),
            ("original_title", "TEXT"),
            ("original_instructions", "TEXT"),
        ],
        "pantry_item": [("use_soon", "BOOLEAN DEFAULT 0")],
        "recipe_ingredient": [("original_raw_text", "VARCHAR(400)")],
    }
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, cols in wanted.items():
        if table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name not in have:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    log.info("Migrace: přidán sloupec %s.%s", table, name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Migrace %s.%s selhala: %s", table, name, exc)


def _load_settings_overrides(db) -> None:
    """Aplikuj runtime nastavení z tabulky app_setting (override env)."""
    try:
        from .models import AppSetting

        rows = db.query(AppSetting).all()
        for row in rows:
            settings.set_admin(row.key, row.value)
        if rows:
            log.info("Načteno %s uložených nastavení.", len(rows))
    except Exception as exc:  # noqa: BLE001
        log.warning("Nepodařilo se načíst nastavení: %s", exc)


app = FastAPI(title="Kuchařka", version="0.1.0")

# CORS – pro odlazení v samostatných WSL prostředích, kdy frontend a API běží
# na různých originech. CORS_ORIGINS = čárkou oddělené originy, "*" = vše (dev).
import os

_origins = os.environ.get("CORS_ORIGINS", "*").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origins == "*" else [o.strip() for o in _origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(recipes.router)
app.include_router(search.router)
app.include_router(ingredients.router)
app.include_router(pantry.router)
app.include_router(mealplan.router)
app.include_router(ingest.router)
app.include_router(receipt.router)
app.include_router(lidl.router)
app.include_router(barcode.router)
app.include_router(hmi.router)
app.include_router(hmi_page.router)
app.include_router(crawl.router)
app.include_router(generate.router)
app.include_router(maintenance.router)
app.include_router(system.router)
app.include_router(admin.router)
app.include_router(auth_router.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    from . import auth as _auth
    from . import scheduler

    db = SessionLocal()
    try:
        _auth.load(db)
        env_pw = os.environ.get("APP_PASSWORD", "").strip()
        if env_pw and not settings.auth_password_hash:
            _auth.set_password(env_pw)
            log.info("Heslo nastaveno z APP_PASSWORD.")
    finally:
        db.close()
    scheduler.configure_all()


@app.middleware("http")
async def _auth_middleware(request, call_next):
    from . import auth as _auth
    from .routers.auth import token_from_request

    path = request.url.path
    protected = path.startswith("/api/") and not (
        path.startswith("/api/auth/")
        or path.startswith("/api/ingest/")
        or path.startswith("/api/hmi/")
        or path == "/api/health"
    )
    if path.startswith("/hmi"):
        protected = False
    if settings.auth_enabled and protected:
        if not _auth.valid_token(token_from_request(request)):
            return JSONResponse({"detail": "Neautorizováno"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "searxng": settings.searxng_enabled,
        "ollama": settings.ollama_enabled,
    }


# --- Nahrané soubory (fotky receptů) --------------------------------------
try:
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
except Exception as exc:  # noqa: BLE001
    log.warning("Nepodařilo se vytvořit UPLOAD_DIR %s: %s", settings.upload_dir, exc)
app.mount(
    "/uploads",
    StaticFiles(directory=settings.upload_dir, check_dir=False),
    name="uploads",
)

# --- Frontend (SPA) -------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=STATIC_DIR / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
