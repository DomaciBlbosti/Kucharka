"""Testy telemetrie LLM volání (spotřeba tokenů, odezva, výpadky).

Hlídá hlavně dvě věci: že se do statistiky dostane, KDO se ptal a KOLIK to
stálo, a že zápis telemetrie nikdy neshodí samotné LLM volání.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-llmstats-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import LlmCall  # noqa: E402
from app.modules import llm_stats, llmclient  # noqa: E402

Base.metadata.create_all(engine)

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


def seed():
    llm_stats.record(component="překlad", provider="api", model="gpt-4o-mini",
                     prompt_tokens=1000, completion_tokens=500, duration_ms=3000)
    llm_stats.record(component="překlad", provider="api", model="gpt-4o-mini",
                     prompt_tokens=2000, completion_tokens=100, duration_ms=1000)
    llm_stats.record(component="tagy", provider="ollama", model="gemma4:12b",
                     prompt_tokens=4000, completion_tokens=200, duration_ms=60000,
                     ok=False, error="timed out")


def main():
    seed()
    s = llm_stats.summary(days=7)
    t = s["totals"]

    check("součet volání", t["calls"] == 3, str(t["calls"]))
    check("počet výpadků", t["failed"] == 1, str(t["failed"]))
    check("součet vstupních tokenů", t["tokens_in"] == 7000, str(t["tokens_in"]))
    check("součet výstupních tokenů", t["tokens_out"] == 800, str(t["tokens_out"]))
    check("max odezva", t["max_ms"] == 60000, str(t["max_ms"]))

    comps = {c["component"]: c for c in s["by_component"]}
    check("rozpad po komponentách", set(comps) == {"překlad", "tagy"}, str(set(comps)))
    check("překlad má 2 volání bez chyby",
          comps["překlad"]["calls"] == 2 and comps["překlad"]["failed"] == 0)
    check("tagy mají zaznamenaný výpadek", comps["tagy"]["failed"] == 1)
    check("průměrná odezva překladu je 2 s", comps["překlad"]["avg_ms"] == 2000,
          str(comps["překlad"]["avg_ms"]))

    models = {m["model"]: m for m in s["by_model"]}
    check("rozpad po modelech vč. providera",
          set(models) == {"gpt-4o-mini (api)", "gemma4:12b (ollama)"}, str(set(models)))

    # cena: jen API se počítá, lokální model je nula
    expect = round(
        (3000 / 1_000_000) * settings.llm_price_in_usd * settings.usd_rate
        + (600 / 1_000_000) * settings.llm_price_out_usd * settings.usd_rate, 2)
    check("odhad ceny jen za API volání",
          models["gpt-4o-mini (api)"]["cost_czk"] == expect,
          f'{models["gpt-4o-mini (api)"]["cost_czk"]} != {expect}')
    check("lokální model se počítá jako nula",
          models["gemma4:12b (ollama)"]["cost_czk"] == 0.0)
    check("celková cena = součet přes modely", s["totals"]["cost_czk"] == expect)

    check("poslední výpadky nesou příčinu",
          len(s["recent_errors"]) == 1 and s["recent_errors"][0]["error"] == "timed out")

    # graf po dnech: jeden den, obě série sečtené dohromady
    check("po dnech je jeden řádek", len(s["by_day"]) == 1, str(len(s["by_day"])))
    check("den sčítá obě série (ollama i api)",
          s["by_day"][0]["tokens_in"] == 7000 and s["by_day"][0]["calls"] == 3)

    # okno „days" musí staré záznamy vynechat
    db = SessionLocal()
    try:
        db.add(LlmCall(ts=datetime.utcnow() - timedelta(days=40), component="staré",
                       provider="api", model="x", prompt_tokens=999_999,
                       completion_tokens=0, duration_ms=1, ok=True))
        db.commit()
    finally:
        db.close()
    check("starší záznamy mimo okno se nepočítají",
          llm_stats.summary(days=7)["totals"]["tokens_in"] == 7000)
    check("delší okno je vidí",
          llm_stats.summary(days=60)["totals"]["tokens_in"] == 1_006_999)

    # prune: retence smaže staré, čerstvé nechá
    old_days = settings.llm_stats_retention_days
    try:
        settings.llm_stats_retention_days = 30
        db = SessionLocal()
        try:
            removed = llm_stats.prune(db)
        finally:
            db.close()
    finally:
        settings.llm_stats_retention_days = old_days
    check("prune smazal jen záznam za retencí", removed == 1, str(removed))
    check("po prune zůstala čerstvá data",
          llm_stats.summary(days=60)["totals"]["tokens_in"] == 7000)

    # MariaDB vrací ze SUM()/AVG() Decimal (SQLite int) – dřív to shodilo
    # celý endpoint na „Decimal * float" (HTTP 500 v kartě Spotřeba LLM)
    class MariaRow:
        calls, failed = 3, Decimal("1")
        tok_in, tok_out = Decimal("7000"), Decimal("800")
        avg_ms, max_ms = Decimal("21333.3333"), Decimal("60000")
        provider = "api"

    try:
        row = llm_stats._row(MariaRow())
        ok_decimal = (
            row["tokens_in"] == 7000 and row["avg_ms"] == 21333
            and isinstance(row["cost_czk"], float) and row["cost_czk"] > 0
        )
        check("Decimal z MariaDB projde výpočtem ceny", ok_decimal, str(row))
    except Exception as exc:  # noqa: BLE001
        check("Decimal z MariaDB projde výpočtem ceny", False, repr(exc))
    check("Decimal u lokálního modelu je nula",
          llm_stats._cost("ollama", Decimal("5000"), Decimal("100")) == 0.0)

    # telemetrie nesmí shodit volání ani při rozbité DB
    orig = llm_stats.SessionLocal
    try:
        llm_stats.SessionLocal = lambda: (_ for _ in ()).throw(RuntimeError("DB je pryč"))
        llm_stats.record(component="x", provider="api", model="y")
        check("výpadek zápisu telemetrie nebublá ven", True)
    except Exception as exc:  # noqa: BLE001
        check("výpadek zápisu telemetrie nebublá ven", False, str(exc))
    finally:
        llm_stats.SessionLocal = orig

    # structured_json předává komponentu do telemetrie
    calls = []
    orig_record = llm_stats.record
    orig_avail = llmclient.settings.llm_provider
    try:
        llm_stats.record = lambda **kw: calls.append(kw)
        llmclient.settings.llm_provider = "ollama"  # bez OLLAMA_URL → rychlé selhání
        llmclient.structured_json("test", component="párování")
        check("neúspěšné volání bez providera telemetrii nezapisuje (vrací dřív)",
              calls == [], str(calls))
    finally:
        llm_stats.record = orig_record
        llmclient.settings.llm_provider = orig_avail

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
