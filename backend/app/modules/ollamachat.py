"""Sdílené volání Ollamy pro strukturované (JSON) odpovědi – text i vision.

Používá /api/chat, ne /api/generate. Důvod: u modelů s "thinking" (Qwen3 a
příbuzné) generate endpoint parametr "think": false ignoruje – model spálí
celý výstupní rozpočet na přemýšlení a pole "response" zůstane prázdné,
i když je HTTP 200 (potvrzený bug, ollama/ollama#14793, #16184). Chat
endpoint s "think" jako parametrem na nejvyšší úrovni odpovědi funguje
správně (response.message.content je vyplněné).
"""
from __future__ import annotations

import logging

import httpx

from .llmjson import parse_json_response

log = logging.getLogger("kucharka.ollamachat")


def chat_json(
    base_url: str,
    model: str,
    prompt: str,
    *,
    images: list[str] | None = None,
    keep_alive: str | None = None,
    timeout: float = 120,
    temperature: float = 0,
) -> dict | None:
    """Zavolej Ollama /api/chat a vrať naparsovaný JSON. None při jakékoli chybě
    (síť, HTTP chyba, nevalidní JSON) – volající si rozhodne o fallbacku."""
    message: dict = {"role": "user", "content": prompt}
    if images:
        message["images"] = images
    payload = {
        "model": model,
        "messages": [message],
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": temperature},
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
        raw = r.json().get("message", {}).get("content", "")
    except Exception as exc:  # noqa: BLE001
        log.warning("Ollama chat volání selhalo (model %s): %s", model, exc)
        return None
    try:
        return parse_json_response(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Ollama chat odpověď (model %s) se nepodařilo naparsovat (%s): %r",
            model, exc, raw[:500],
        )
        return None
