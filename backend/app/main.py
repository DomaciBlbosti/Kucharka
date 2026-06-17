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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import crawl, ingredients, pantry, recipes, search
from .seed.starter_ingredients import seed_starter

log = logging.getLogger("kucharka")
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def init_db(retries: int = 10) -> None:
    """Počkej na DB (MariaDB při startu kontejneru naběhne se zpožděním)."""
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(engine)
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
    finally:
        db.close()


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
app.include_router(crawl.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    if settings.crawler_enabled:
        _start_scheduler()


def _start_scheduler() -> None:
    """Spustí periodický crawl na pozadí (volitelné, CRAWLER_ENABLED=true)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        from .modules import crawler
    except Exception as exc:  # noqa: BLE001
        log.warning("Plánovač nelze spustit: %s", exc)
        return

    sched = BackgroundScheduler(daemon=True)
    sched.add_job(
        lambda: crawler.crawl(max_recipes=settings.crawler_max_per_run),
        "interval",
        minutes=settings.crawler_interval_min,
        next_run_time=None,  # první běh až po intervalu, ne hned při startu
        id="crawler",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    log.info(
        "Crawler naplánován každých %s min (max %s receptů/běh).",
        settings.crawler_interval_min,
        settings.crawler_max_per_run,
    )
    app.state.scheduler = sched


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "searxng": settings.searxng_enabled,
        "ollama": settings.ollama_enabled,
    }


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
