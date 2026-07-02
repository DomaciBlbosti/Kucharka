"""Jádro (core) – samostatná služba. Servíruje lehké admin rozhraní a řídí
úlohy (crawl / překlad / kategorizace), které výsledky posílají do webové
appky přes ingest kontrakt. Konfiguruje se přes WEB_API_URL.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import config, jobs, webclient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("core")

app = FastAPI(title="Kuchařka – jádro")
_HTML = (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")


@app.on_event("startup")
def _startup():
    jobs.configure_schedule()


@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML


@app.get("/api/config")
def get_config():
    c = config.get()
    c["fast_model_effective"] = config.fast_model()
    return c


class ConfigUpdate(BaseModel):
    values: dict


@app.put("/api/config")
def put_config(req: ConfigUpdate):
    c = config.update(req.values)
    jobs.configure_schedule()
    c["fast_model_effective"] = config.fast_model()
    return c


@app.get("/api/test/web")
def test_web():
    try:
        return {"ok": True, "result": webclient.ping()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/api/test/ollama")
def test_ollama():
    c = config.get()
    try:
        r = httpx.get(f"{c['ollama_url'].rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    fm = config.fast_model()
    base = fm.split(":")[0]
    has = any(m == fm or m.split(":")[0] == base for m in models)
    return {"ok": True, "models": models, "fast_model": fm, "has_fast_model": has}


@app.post("/api/run/crawl")
def run_crawl():
    return {"started": jobs.start_crawl(), "status": jobs.status()}


@app.post("/api/run/translate")
def run_translate():
    return {"started": jobs.start_translate(), "status": jobs.status()}


@app.post("/api/run/categorize")
def run_categorize():
    return {"started": jobs.start_categorize(), "status": jobs.status()}


@app.get("/api/status")
def status():
    return jobs.status()
