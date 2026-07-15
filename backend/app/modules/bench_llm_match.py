"""Benchmark LLM matchingu (Ollama) — najde nejlepší kombinaci num_ctx /
temperature / batch size na vzorku, kde už znáš správnou odpověď.

Princip: vezmi N náhodných řádků `recipe_ingredient`, které JSOU napárované
(`ingredient_id IS NOT NULL`), předstírej, že napárované nejsou (pošli LLM
jen `raw_text`), a porovnej, co vrátí, s tím, co ve skutečnosti měly. Tak
dostaneš skutečnou accuracy na produkčních datech, ne na vymyšlených
příkladech — a je to bezpečné, protože se do DB nic nezapisuje.

Spuštění (z backend/, aktivní venv):
    python -m app.modules.bench_llm_match --sample 200

Výstup: tabulka combo × accuracy × avg_latency, seřazená od nejlepší.
"""
from __future__ import annotations

import argparse
import itertools
import json
import time

import httpx
from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..models import RecipeIngredient
from .llm_match import _RESPONSE_SCHEMA, _build_ingredient_catalog, _make_prompt


def _sample(db, n: int) -> list[tuple[str, int]]:
    """Vrátí (raw_text, opravdové ingredient_id) pro n náhodných napárovaných řádků."""
    rows = db.execute(
        select(RecipeIngredient.raw_text, RecipeIngredient.ingredient_id)
        .where(RecipeIngredient.ingredient_id.is_not(None))
        .order_by(func.rand())
        .limit(n)
    ).all()
    return [(r.raw_text, r.ingredient_id) for r in rows if r.raw_text]


def _call(prompt: str, model: str, num_ctx: int, temperature: float) -> tuple[dict | None, float]:
    t0 = time.perf_counter()
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": _RESPONSE_SCHEMA,
                "think": False,
                "keep_alive": settings.ollama_keep_alive,
                "options": {"temperature": temperature, "num_ctx": num_ctx},
            },
            timeout=180,
        )
        r.raise_for_status()
        out = json.loads(r.json()["response"])
    except Exception as exc:  # noqa: BLE001
        print(f"  chyba volání: {exc}")
        return None, time.perf_counter() - t0
    return out, time.perf_counter() - t0


def run(sample_size: int, batch_sizes: list[int], num_ctxs: list[int], temps: list[float], model: str) -> None:
    db = SessionLocal()
    catalog = _build_ingredient_catalog(db)
    sample = _sample(db, sample_size)
    print(f"Vzorek: {len(sample)} řádků, katalog: {len(catalog)} surovin, model: {model}\n")

    results = []
    for bs, num_ctx, temp in itertools.product(batch_sizes, num_ctxs, temps):
        correct = 0
        total = 0
        latencies = []
        for start in range(0, len(sample), bs):
            chunk = sample[start:start + bs]
            inputs = [raw for raw, _ in chunk]
            truth = {raw: iid for raw, iid in chunk}

            out, elapsed = _call(_make_prompt(catalog, inputs), model, num_ctx, temp)
            latencies.append(elapsed)
            if out is None:
                total += len(chunk)
                continue
            got = {it.get("input"): it.get("ingredient_id") for it in out.get("items", [])}
            for raw in inputs:
                total += 1
                if got.get(raw) == truth[raw]:
                    correct += 1

        acc = correct / total if total else 0.0
        avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
        results.append((bs, num_ctx, temp, acc, avg_lat, total))
        print(f"bs={bs:>3} num_ctx={num_ctx:>6} temp={temp:.1f}  "
              f"accuracy={acc:.1%}  avg_latency={avg_lat:.1f}s  (n={total})")

    print("\n--- Seřazeno podle přesnosti ---")
    for bs, num_ctx, temp, acc, avg_lat, total in sorted(results, key=lambda r: -r[3]):
        print(f"bs={bs:>3} num_ctx={num_ctx:>6} temp={temp:.1f}  "
              f"accuracy={acc:.1%}  avg_latency={avg_lat:.1f}s")

    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--batch-sizes", type=int, nargs="+", default=[15, 40])
    ap.add_argument("--num-ctx", type=int, nargs="+", default=[settings.llm_match_num_ctx])
    ap.add_argument("--temps", type=float, nargs="+", default=[0.0, 0.3])
    ap.add_argument("--model", default=settings.ollama_model)
    args = ap.parse_args()
    run(args.sample, args.batch_sizes, args.num_ctx, args.temps, args.model)
