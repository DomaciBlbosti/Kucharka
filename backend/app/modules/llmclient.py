"""Jednotný vstup pro strukturovaná (JSON) LLM volání dávkových úloh.

Dávkové úlohy (párování surovin, tagování, kategorizace) volají tento modul
místo přímého `ollamachat.chat_json`. Podle `settings.llm_provider` se dotaz
pošle buď na lokální Ollamu (default, beze změny chování), nebo na komerční
OpenAI-kompatibilní API (`/chat/completions` – OpenAI, DeepSeek, Groq,
Mistral, OpenRouter…). Lokální GPU pak zůstává jen na embeddingy/OCR/RAG.

Proč: gemma na 12GB VRAM zvládá dávkové párování pomalu a nespolehlivě.
Mini-class komerční modely jsou na tenhle typ úlohy výrazně přesnější a
při dávkách po ~40 surovinách stojí zpracování celé fronty řádově dolary.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager

import httpx

from ..config import settings
from .llmjson import parse_json_response
from .ollamachat import chat_json_raw

log = logging.getLogger("kucharka.llmclient")

# Trasování dotazů a odpovědí LLM do logu (Admin → Služby na pozadí → chip
# „LLM"). Náhledy se ořezávají na jeden řádek, ať je vidět CO odchází a CO
# se vrací – bez toho se nedá poznat, jestli fronta neubývá kvůli chybám,
# zahazovaným odpovědím, nebo jen pomalému tempu.
_trace = logging.getLogger("kucharka.llm")


def _one_line(s, limit: int = 220) -> str:
    out = " ".join(str(s or "").split())
    return out[:limit] + ("…" if len(out) > limit else "")

# Globální zámek na Ollamu pro DÁVKOVÉ úlohy (párování, kategorie, tagy,
# hromadný překlad). Plánovač sice úlohy serializuje sám (jeden worker), ale
# ruční tlačítko v administraci spuštěné během běžícího automatu by poslalo
# na GPU druhý proud požadavků a oba by se vyhladověly do timeoutu. Se
# zámkem jde na lokální GPU vždy jen jedno dávkové volání po druhém, ať se
# úlohy spustí odkudkoli. Interaktivní cesty (generování receptu, OCR) se
# záměrně negatují – krátké jednotlivé dotazy nemají čekat za dávkou.
_ollama_gate = threading.Lock()


@contextmanager
def ollama_gate():
    """Použij kolem Ollama volání dávkové úlohy (komerční API zámek nepotřebuje)."""
    with _ollama_gate:
        yield

# Poslední chyba LLM volání – volající (llm_match) ji ukládá k rozhodnutím
# a do stavu běhu, ať je v UI vidět SKUTEČNÁ příčina ("timeout", "connection
# refused", "model not found"…), ne jen generické "volání selhalo".
_last_error: str | None = None


def last_error() -> str | None:
    return _last_error


def _set_error(msg: str | None) -> None:
    global _last_error
    _last_error = msg[:300] if msg else None


def availability_error() -> str | None:
    """None, když je zvolený provider použitelný; jinak lidská hláška."""
    if settings.llm_provider == "api":
        if not settings.llm_api_key:
            return "Komerční LLM API je zvolené, ale chybí API klíč (Administrace → Nástroje)."
        if not settings.llm_api_url:
            return "Komerční LLM API je zvolené, ale chybí URL."
        return None
    if not settings.ollama_enabled:
        return "Ollama není dostupná (OLLAMA_URL)."
    return None


def is_available() -> bool:
    return availability_error() is None


def active_model(ollama_model: str | None = None) -> str:
    """Název modelu, který by structured_json použil (pro log/decision záznamy)."""
    if settings.llm_api_enabled:
        return settings.llm_api_model
    return ollama_model or settings.ollama_fast_model


def _finish(
    *, component: str, provider: str, model: str, t0: float,
    out: dict | None, usage: dict, fail_detail: str,
) -> None:
    """Společný závěr volání: trasovací log + zápis do telemetrie."""
    dt = time.monotonic() - t0
    ok = out is not None
    if ok:
        _trace.info("← %s | %.1f s | %s", model, dt,
                    _one_line(json.dumps(out, ensure_ascii=False)))
    else:
        _trace.warning("← %s | %.1f s | SELHALO: %s", model, dt, _one_line(fail_detail))
    from . import llm_stats  # lazy: telemetrie sahá do DB, llmclient se importuje brzy

    llm_stats.record(
        component=component, provider=provider, model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        duration_ms=int(dt * 1000), ok=ok,
        error=None if ok else fail_detail,
    )


def structured_json(
    prompt: str,
    *,
    schema: dict | None = None,
    timeout: float = 120,
    temperature: float = 0,
    num_ctx: int | None = None,
    ollama_model: str | None = None,
    component: str = "ostatní",
) -> dict | None:
    """Vrátí naparsovaný JSON, nebo None při jakékoli chybě (volající má fallback).

    `num_ctx` a `ollama_model` se týkají jen Ollamy – komerční API má kontext
    dost velký a model globálně nastavený (`settings.llm_api_model`).
    `component` říká, kdo se ptá (překlad / kategorie / tagy / párování …) –
    slouží jen telemetrii v Admin → Spotřeba LLM.
    """
    model = active_model(ollama_model)
    _trace.info("→ %s | %s zn | %s", model, len(prompt), _one_line(prompt))
    t0 = time.monotonic()
    usage: dict = {}

    if settings.llm_api_enabled:
        out = _api_chat_json(
            prompt, schema=schema, timeout=timeout, temperature=temperature,
            usage_out=usage,
        )
        _finish(component=component, provider="api", model=model, t0=t0, out=out,
                usage=usage, fail_detail=last_error() or "bez detailu")
        return out

    if not settings.ollama_enabled:
        _set_error("Ollama není nakonfigurovaná (OLLAMA_URL).")
        return None
    with _ollama_gate:
        parsed, raw = chat_json_raw(
            settings.ollama_url,
            ollama_model or settings.ollama_fast_model,
            prompt,
            keep_alive=settings.ollama_keep_alive,
            timeout=timeout,
            temperature=temperature,
            format_schema=schema,
            num_ctx=num_ctx,
            usage_out=usage,
        )
    if parsed is None:
        # raw je buď "<chyba volání: …>" (síť/HTTP), nebo neparsovatelná odpověď
        _set_error(raw or "prázdná odpověď modelu")
    else:
        _set_error(None)
    _finish(component=component, provider="ollama", model=model, t0=t0, out=parsed,
            usage=usage, fail_detail=raw or "prázdná odpověď modelu")
    return parsed


def _api_chat_json(
    prompt: str,
    *,
    schema: dict | None,
    timeout: float,
    temperature: float,
    usage_out: dict | None = None,
) -> dict | None:
    """OpenAI-kompatibilní /chat/completions se strukturovaným výstupem.

    Nejdřív zkusí `response_format: json_schema` (OpenAI, novější provideri);
    když ho server odmítne (HTTP 4xx), spadne na obecnější `json_object`,
    který umí prakticky každý – struktura je i tak popsaná v promptu.
    """
    formats: list[dict] = []
    if schema is not None:
        formats.append({
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        })
    formats.append({"type": "json_object"})

    for i, fmt in enumerate(formats):
        payload = {
            "model": settings.llm_api_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "response_format": fmt,
        }
        try:
            r = httpx.post(
                f"{settings.llm_api_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                timeout=timeout,
            )
            r.raise_for_status()
            body = r.json()
            raw = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if usage_out is not None:
                # OpenAI-kompatibilní odpověď nese spotřebu v `usage`
                u = body.get("usage") or {}
                usage_out["prompt_tokens"] = int(u.get("prompt_tokens") or 0)
                usage_out["completion_tokens"] = int(u.get("completion_tokens") or 0)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500 and i + 1 < len(formats):
                log.info(
                    "LLM API odmítlo response_format=%s (HTTP %s), zkouším %s",
                    fmt.get("type"), code, formats[i + 1].get("type"),
                )
                continue
            body = exc.response.text[:300]
            log.warning("LLM API volání selhalo (HTTP %s): %s", code, body)
            _set_error(f"HTTP {code}: {body}")
            return None
        except Exception as exc:  # noqa: BLE001 - síť, timeout…
            log.warning("LLM API volání selhalo: %s", exc)
            _set_error(str(exc))
            return None
        try:
            out = parse_json_response(raw)
            _set_error(None)
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM API odpověď není validní JSON (%s): %r", exc, raw[:300])
            _set_error(f"nevalidní JSON: {str(exc)[:120]}")
            return None
    return None


def test_call() -> dict:
    """Diagnostika komerčního API pro admin UI: mini strukturované volání."""
    if not settings.llm_api_key:
        return {"ok": False, "error": "API klíč není nastavený."}
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    out = _api_chat_json(
        'Odpověz JSON objektem {"answer": 2+2}.',
        schema=schema, timeout=30, temperature=0,
    )
    if out is None:
        return {"ok": False, "error": "Volání selhalo – detail v logu (Služby na pozadí)."}
    return {"ok": True, "model": settings.llm_api_model, "answer": out.get("answer")}
