"""Konfigurace jádra (core). Jádro nemá DB – nastavení drží v JSON souboru
ve volume, plus výchozí hodnoty z prostředí. Web API (kuchařka) se nastaví
přes WEB_API_URL, výchozí je stejný server přes 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CORE_CONFIG", "/config/core.json"))

_DEFAULTS = {
    "web_api_url": os.environ.get("WEB_API_URL", "http://127.0.0.1:8000"),
    "core_token": os.environ.get("CORE_TOKEN", ""),
    "ollama_url": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
    "ollama_model": os.environ.get("OLLAMA_MODEL", "qwen3:14b"),
    "ollama_fast_model": os.environ.get("OLLAMA_FAST_MODEL", ""),
    "ollama_keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
    "bg_workers": int(os.environ.get("BG_WORKERS", "2")),
    "recipe_domains": os.environ.get(
        "RECIPE_DOMAINS",
        "recepty.cz,toprecepty.cz,apetitonline.cz,vareni.cz,ireceptar.cz,"
        "klasicke-recepty.cz,bestrecepty.cz,kucharky.cz",
    ),
    "scraper_verify_ssl": os.environ.get("SCRAPER_VERIFY_SSL", "true") == "true",
    "translate_to_cs": True,
    # plánované služby na pozadí
    "crawler_enabled": False,
    "crawler_interval_min": 360,
    "crawler_max_per_run": 30,
    "auto_translate_enabled": False,
    "auto_translate_interval_min": 180,
    "auto_categorize_enabled": False,
    "auto_categorize_interval_min": 360,
}

_lock = threading.Lock()
_data: dict = {}


def _load() -> None:
    global _data
    d = dict(_DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            d.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:  # noqa: BLE001
        pass
    _data = d


def get() -> dict:
    with _lock:
        if not _data:
            _load()
        return dict(_data)


def update(values: dict) -> dict:
    with _lock:
        if not _data:
            _load()
        for k, v in values.items():
            if k in _DEFAULTS:
                _data[k] = v
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(_data, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass
        return dict(_data)


def fast_model() -> str:
    c = get()
    return c["ollama_fast_model"] or c["ollama_model"]
