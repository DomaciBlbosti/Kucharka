"""Katalog modelů z ollamaproxy a směrování komerčních volání.

Proxy (`GET /mgmt/v1/models`) vrací pro každý backend seznam modelů:
    {"ollama": {"models": [...], "base_url": ".../"},
     "openai": {"kind": "openai", "base_url": ".../providers/openai/v1", "models": [...]},
     "anthropic": {"kind": "anthropic", "base_url": ".../providers/anthropic", ...}}
Komerční poskytovatelé mají každý vlastní cestu a formát – proxy nic
nepřekládá. Tenhle modul si katalog drží (TTL) a pro zadaný název modelu
řekne, kam a jakým protokolem volat. Model mimo katalog jde na `{proxy}/v1`
(lokální Ollama přes OpenAI-kompatibilní vrstvu) – tak to fungovalo dřív.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from ..config import settings

log = logging.getLogger("kucharka.proxy_catalog")

TTL_S = 600
# Protokoly, kterými umí llmclient mluvit. `google` zatím ne.
CHAT_KINDS = ("openai", "anthropic", "ollama", "gpu")
EMBED_KINDS = ("openai", "ollama", "gpu")

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "backends": {}, "error": None}


def _base_for(kind: str, base_url: str) -> str:
    """OpenAI-kompatibilní kořen (…/v1) pro openai/ollama/gpu; anthropic nechá nativní."""
    b = base_url.rstrip("/")
    if kind == "anthropic":
        return b
    return b if b.endswith("/v1") else b + "/v1"


def fetch(force: bool = False) -> dict:
    """{slug: {"kind", "base_url", "models": [...], "ok", "error"}} – s cache."""
    now = time.time()
    if not force and _cache["backends"] and now - _cache["ts"] < TTL_S:
        return _cache["backends"]
    with _lock:
        if not force and _cache["backends"] and time.time() - _cache["ts"] < TTL_S:
            return _cache["backends"]
        url = settings.ollama_url.rstrip("/")
        if not url:
            _cache.update(ts=time.time(), backends={}, error="OLLAMA_URL není nastavená")
            return {}
        try:
            r = httpx.get(f"{url}/mgmt/v1/models", headers=settings.ollama_headers(), timeout=20)
            r.raise_for_status()
            raw = r.json()
            backends: dict = {}
            for slug, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                kind = "ollama" if slug == "ollama" else str(entry.get("kind") or "openai")
                base = entry.get("base_url") or (url if slug == "ollama" else f"{url}/providers/{slug}")
                backends[slug] = {
                    "kind": kind,
                    "base_url": _base_for(kind, str(base)),
                    "models": sorted(m for m in (entry.get("models") or []) if isinstance(m, str) and m),
                    "ok": bool(entry.get("ok", True)),
                    "error": entry.get("error"),
                }
            _cache.update(ts=time.time(), backends=backends, error=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("Katalog modelů z proxy se nepodařilo načíst: %s", exc)
            _cache.update(ts=time.time(), error=str(exc)[:300])
            # starý katalog ponechat, je lepší než nic
        return _cache["backends"]


def last_error() -> str | None:
    return _cache["error"]


def backend_for(model: str) -> tuple[str, str]:
    """(kind, base_url) pro model. Lokální Ollama vítězí nad shodou u poskytovatele."""
    backends = fetch()
    local = backends.get("ollama")
    if local and model in local["models"]:
        return "openai", local["base_url"]
    for slug, b in backends.items():
        if slug != "ollama" and model in b["models"]:
            return b["kind"], b["base_url"]
    return "openai", f"{settings.ollama_url.rstrip('/')}/v1"


def groups(kinds: tuple[str, ...] = CHAT_KINDS) -> list[dict]:
    """Pro administraci: [{provider, kind, models}] jen s použitelnými protokoly."""
    out = []
    for slug, b in fetch().items():
        if b["kind"] in kinds and b["models"]:
            out.append({"provider": slug, "kind": b["kind"], "models": b["models"]})
    out.sort(key=lambda g: (g["provider"] != "ollama", g["provider"]))
    return out
