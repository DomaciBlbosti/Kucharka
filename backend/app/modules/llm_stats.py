"""Telemetrie LLM volání: spotřeba tokenů, doba odezvy a výpadky.

Každé dávkové volání (překlad, párování, kategorie, tagy) zapíše jeden řádek
do `llm_call` – kdo se ptal, jaký model odpověděl, kolik to stálo tokenů, jak
dlouho to trvalo a jestli to prošlo. Z toho se v administraci skládá graf
spotřeby po dnech a tabulky po komponentách a modelech.

Zásady:
  * Zápis nesmí NIKDY shodit samotné volání – appka podle téhle tabulky nic
    neřídí, je to čistě provozní přehled. Proto je celé `record()` v try/except.
  * Tabulka nesmí růst donekonečna – `prune()` maže záznamy starší než
    `llm_stats_retention_days` a spouští se nejvýš jednou za hodinu.
  * Cena je ODHAD: tokeny × sazba z nastavení (výchozí gpt-4o-mini). Lokální
    Ollama je vždy za nula – elektřina se tudy počítat nedá.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select

from ..config import settings
from ..db import SessionLocal
from ..models import LlmCall

log = logging.getLogger("kucharka.llm_stats")

_PRUNE_EVERY_S = 3600.0
_last_prune = 0.0
_prune_lock = threading.Lock()


def prune(db) -> int:
    """Smaž záznamy starší než retence. Vrací počet smazaných."""
    days = settings.llm_stats_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    removed = db.execute(delete(LlmCall).where(LlmCall.ts < cutoff)).rowcount
    db.commit()
    if removed:
        log.info("llm_stats: smazáno %s záznamů starších než %s dní", removed, days)
    return removed


def _maybe_prune(db) -> None:
    """`prune`, ale nejvýš jednou za hodinu – běží v rámci zápisu, ať kvůli
    úklidu nepotřebujeme vlastní úlohu v plánovači."""
    global _last_prune
    if settings.llm_stats_retention_days <= 0:
        return
    now = time.monotonic()
    with _prune_lock:
        if _last_prune and now - _last_prune < _PRUNE_EVERY_S:
            return
        _last_prune = now
    prune(db)


def record(
    *,
    component: str,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    duration_ms: int = 0,
    ok: bool = True,
    error: str | None = None,
) -> None:
    """Zapiš jedno LLM volání. Chyba zápisu se jen zaloguje – nikdy nebublá ven."""
    db = None
    try:
        db = SessionLocal()
        db.add(LlmCall(
            component=(component or "?")[:40],
            provider=(provider or "?")[:20],
            model=(model or "?")[:120],
            prompt_tokens=max(0, int(prompt_tokens or 0)),
            completion_tokens=max(0, int(completion_tokens or 0)),
            duration_ms=max(0, int(duration_ms or 0)),
            ok=bool(ok),
            error=(error or None) and str(error)[:300],
        ))
        db.commit()
        _maybe_prune(db)
    except Exception as exc:  # noqa: BLE001 - telemetrie nesmí shodit volání
        log.warning("llm_stats: zápis selhal (%s)", exc)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if db is not None:
            db.close()


def _cost(provider: str, prompt_tokens: float, completion_tokens: float) -> float:
    """Odhad ceny v Kč. Lokální model = 0 (platí se elektřinou, ne tokeny)."""
    if provider != "api":
        return 0.0
    per_mtok_in = settings.llm_price_in_usd * settings.usd_rate
    per_mtok_out = settings.llm_price_out_usd * settings.usd_rate
    return round(
        (prompt_tokens / 1_000_000) * per_mtok_in
        + (completion_tokens / 1_000_000) * per_mtok_out,
        2,
    )


_AGG = (
    func.count(LlmCall.id).label("calls"),
    func.sum(case((LlmCall.ok.is_(False), 1), else_=0)).label("failed"),
    func.coalesce(func.sum(LlmCall.prompt_tokens), 0).label("tok_in"),
    func.coalesce(func.sum(LlmCall.completion_tokens), 0).label("tok_out"),
    func.coalesce(func.avg(LlmCall.duration_ms), 0).label("avg_ms"),
    func.coalesce(func.max(LlmCall.duration_ms), 0).label("max_ms"),
)


def _row(r, label_field: str | None = None, label: str | None = None) -> dict:
    provider = getattr(r, "provider", "api")
    out = {
        "calls": int(r.calls or 0),
        "failed": int(r.failed or 0),
        "tokens_in": int(r.tok_in or 0),
        "tokens_out": int(r.tok_out or 0),
        "avg_ms": int(r.avg_ms or 0),
        "max_ms": int(r.max_ms or 0),
        "cost_czk": _cost(provider, r.tok_in or 0, r.tok_out or 0),
    }
    if label_field:
        out[label_field] = label
    return out


def summary(days: int = 14) -> dict:
    """Souhrn za posledních `days` dní: po dnech (graf), po komponentách a po
    modelech (tabulky), plus celkové součty."""
    since = datetime.utcnow() - timedelta(days=max(1, days))
    db = SessionLocal()
    try:
        base = select(*_AGG).where(LlmCall.ts >= since)

        day = func.date(LlmCall.ts).label("day")
        by_day = [
            {"day": str(r.day), **_row(r)}
            for r in db.execute(
                select(day, LlmCall.provider, *_AGG)
                .where(LlmCall.ts >= since)
                .group_by(day, LlmCall.provider)
                .order_by(day)
            ).all()
        ]
        by_component = [
            _row(r, "component", r.component)
            for r in db.execute(
                select(LlmCall.component, LlmCall.provider, *_AGG)
                .where(LlmCall.ts >= since)
                .group_by(LlmCall.component, LlmCall.provider)
                .order_by(func.count(LlmCall.id).desc())
            ).all()
        ]
        by_model = [
            _row(r, "model", f"{r.model} ({r.provider})")
            for r in db.execute(
                select(LlmCall.model, LlmCall.provider, *_AGG)
                .where(LlmCall.ts >= since)
                .group_by(LlmCall.model, LlmCall.provider)
                .order_by(func.count(LlmCall.id).desc())
            ).all()
        ]
        totals = _row(db.execute(base).one())
        # celková cena = součet přes modely (lokální jsou nula, viz _cost)
        totals["cost_czk"] = round(sum(m["cost_czk"] for m in by_model), 2)

        # poslední výpadky – ať je vidět, čím LLM padá
        recent_errors = [
            {
                "ts": r.ts.isoformat() if r.ts else None,
                "component": r.component,
                "model": r.model,
                "error": r.error,
            }
            for r in db.execute(
                select(LlmCall.ts, LlmCall.component, LlmCall.model, LlmCall.error)
                .where(LlmCall.ts >= since, LlmCall.ok.is_(False))
                .order_by(LlmCall.ts.desc())
                .limit(10)
            ).all()
        ]
    finally:
        db.close()

    return {
        "days": days,
        "totals": totals,
        "by_day": _merge_days(by_day),
        "by_component": by_component,
        "by_model": by_model,
        "recent_errors": recent_errors,
        "price_note": (
            f"odhad podle {settings.llm_price_in_usd} / {settings.llm_price_out_usd} "
            f"USD za 1M tokenů (vstup/výstup), kurz {settings.usd_rate} Kč; "
            "lokální Ollama se počítá jako 0"
        ),
    }


def _merge_days(rows: list[dict]) -> list[dict]:
    """Den může mít víc řádků (Ollama i API) – pro graf je sečti do jednoho."""
    merged: dict[str, dict] = {}
    for r in rows:
        cur = merged.setdefault(r["day"], {k: 0 for k in r if k != "day"} | {"day": r["day"]})
        for k, v in r.items():
            if k == "day":
                continue
            cur[k] = max(cur[k], v) if k == "max_ms" else cur[k] + v
    # avg_ms se sečíst nedá – přepočti jako vážený průměr přes počet volání
    for day, cur in merged.items():
        same = [r for r in rows if r["day"] == day]
        calls = sum(r["calls"] for r in same) or 1
        cur["avg_ms"] = int(sum(r["avg_ms"] * r["calls"] for r in same) / calls)
        cur["cost_czk"] = round(cur["cost_czk"], 2)
    return [merged[d] for d in sorted(merged)]
