"""Audit slovníku surovin – READ-ONLY hledání duplicit.

Podezření: 12 tisíc surovin je na domácí kuchařku moc. Slovník plnily čtyři
nezávislé cesty (NutriDatabáze, LLM odhady, účtenky, čárové kódy, ruční
zadání), `ingredient.name_cs` nemá UNIQUE a obě cesty, které surovinu
zakládaly, kontrolovaly existenci jen přes přesnou shodu názvu. Duplicity
proto vznikat mohly a tenhle modul je spočítá.

Nic nemaže a nic neslučuje – jen vyexportuje shluky k posouzení. Slučování
je destruktivní (přepojení receptů, spíže, nákupu) a patří až za ruční
kontrolu; automat by spojil „smetanu ke šlehání" se „zakysanou smetanou".

Shluky se hledají ve třech vrstvách, každá volnější než předchozí:
  1. shodný název (case-insensitive) – tvrdá duplicita, nemá co existovat,
  2. shodný normalizovaný klíč (stemmer + seřazená slova) – „paprika mletá
     sladká" vs „mletá sladká paprika",
  3. fuzzy podobnost klíčů (rapidfuzz) – překlepy a drobné odchylky.

Spuštění z CLI (z adresáře backend/):
    python -m app.modules.ingredient_audit
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timezone

from rapidfuzz import fuzz, process
from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Ingredient, IngredientAlias, PantryItem, RecipeIngredient
from .corpus_audit import ANALYSIS_DIR
from .ingredient_resolve import NUTRIDB_SOURCE, name_key

log = logging.getLogger("kucharka.ingredient_audit")

REPORT_PATH = ANALYSIS_DIR / "ingredient_audit.json"

# Práh pro třetí vrstvu. Níž než u slučování v `ingredient_resolve` – tady
# jde o NÁVRHY k posouzení, ne o automatické spojení.
_FUZZY_CUTOFF = 88
_MAX_CLUSTERS = 400   # do exportu; celkový počet se hlásí vždy


_lock = threading.Lock()
_state: dict = {
    "running": False, "phase": None, "done": 0, "total": 0,
    "error": None, "finished_at": None, "duration_s": None,
}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["report_exists"] = REPORT_PATH.exists()
    s["report_bytes"] = REPORT_PATH.stat().st_size if REPORT_PATH.exists() else 0
    s["report_mtime"] = (
        datetime.fromtimestamp(REPORT_PATH.stat().st_mtime, tz=timezone.utc).isoformat()
        if REPORT_PATH.exists() else None
    )
    return s


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def _usage(db) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
    """Kolik receptů / řádků spíže / aliasů visí na které surovině.

    Bez toho se u shluku nedá rozhodnout, který záznam je ten „hlavní" a co
    by se sloučením ztratilo."""
    recipes = dict(db.execute(
        select(RecipeIngredient.ingredient_id, func.count(func.distinct(RecipeIngredient.recipe_id)))
        .where(RecipeIngredient.ingredient_id.is_not(None))
        .group_by(RecipeIngredient.ingredient_id)
    ).all())
    pantry = dict(db.execute(
        select(PantryItem.ingredient_id, func.count())
        .group_by(PantryItem.ingredient_id)
    ).all())
    aliases = dict(db.execute(
        select(IngredientAlias.ingredient_id, func.count())
        .where(IngredientAlias.ingredient_id.is_not(None))
        .group_by(IngredientAlias.ingredient_id)
    ).all())
    return recipes, pantry, aliases


def run() -> dict:
    """Jeden průchod slovníkem. Read-only: session jen čte."""
    started = time.monotonic()
    db = SessionLocal()
    try:
        _set(phase="načítám", done=0, total=0)
        rows = db.execute(
            select(Ingredient.id, Ingredient.name_cs, Ingredient.source,
                   Ingredient.kcal_100g, Ingredient.category_path)
        ).all()
        total = len(rows)
        _set(total=total)
        recipes, pantry, aliases = _usage(db)

        by_source = Counter((r.source or "?") for r in rows)
        no_kcal = sum(1 for r in rows if r.kcal_100g is None)
        unused = sum(1 for r in rows if not recipes.get(r.id))

        def info(r) -> dict:
            return {
                "id": r.id,
                "name": r.name_cs,
                "source": r.source,
                "kcal_100g": r.kcal_100g,
                "category_path": r.category_path,
                "recipes": recipes.get(r.id, 0),
                "pantry": pantry.get(r.id, 0),
                "aliases": aliases.get(r.id, 0),
                "reference": r.source == NUTRIDB_SOURCE,
            }

        _set(phase="shluky")
        by_id = {r.id: r for r in rows}
        exact: dict[str, list[int]] = {}
        keys: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            if not r.name_cs:
                continue
            exact.setdefault(r.name_cs.strip().lower(), []).append(r.id)
            keys.setdefault(name_key(r.name_cs), []).append(r.id)
            if i % 500 == 0:
                _set(done=i)

        clusters: list[dict] = []
        seen: set[int] = set()

        def add_cluster(kind: str, ids: list[int], detail: str = "") -> None:
            fresh = [i for i in ids if i not in seen]
            if len(fresh) < 2:
                return
            seen.update(fresh)
            members = sorted(
                (info(by_id[i]) for i in fresh),
                key=lambda m: (not m["reference"], -m["recipes"], m["id"]),
            )
            clusters.append({
                "kind": kind,
                "detail": detail,
                "size": len(members),
                "recipes_total": sum(m["recipes"] for m in members),
                "has_reference": any(m["reference"] for m in members),
                # První člen je návrh na „vítěze": referenční z NutriDatabáze,
                # jinak ten s nejvíc recepty. Rozhodnutí zůstává na člověku.
                "suggested_keep": members[0]["id"],
                "members": members,
            })

        for name, ids in exact.items():
            add_cluster("shodný název", ids, name)
        for key, ids in keys.items():
            add_cluster("shodný klíč", ids, key)

        # Třetí vrstva: fuzzy mezi klíči, které samy o sobě shluk netvoří.
        _set(phase="fuzzy")
        singles = [k for k, ids in keys.items() if len(ids) == 1 and ids[0] not in seen]
        for i, key in enumerate(singles):
            if i % 500 == 0:
                _set(done=i, total=len(singles))
            if keys[key][0] in seen or not key:
                continue
            near = process.extract(
                key, singles, scorer=fuzz.token_sort_ratio,
                score_cutoff=_FUZZY_CUTOFF, limit=6,
            )
            ids = [keys[k][0] for k, _score, _idx in near]
            if len(ids) > 1:
                add_cluster("podobný název", ids, key)

        clusters.sort(key=lambda c: (-c["recipes_total"], -c["size"]))
        dup_rows = sum(c["size"] - 1 for c in clusters)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_ingredients": total,
            "by_source": dict(by_source.most_common()),
            "without_kcal": no_kcal,
            "unused_in_recipes": unused,
            "clusters_total": len(clusters),
            "duplicate_rows": dup_rows,
            "duplicate_pct": round(100.0 * dup_rows / total, 1) if total else 0.0,
            "clusters_by_kind": dict(Counter(c["kind"] for c in clusters)),
            "clusters": clusters[:_MAX_CLUSTERS],
            "clusters_truncated": max(0, len(clusters) - _MAX_CLUSTERS),
        }
        ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        duration = round(time.monotonic() - started, 1)
        _set(error=None, duration_s=duration)
        log.info("audit surovin hotový za %s s (%s surovin, %s shluků, %s duplicit)",
                 duration, total, len(clusters), dup_rows)
        return {
            "total_ingredients": total, "clusters": len(clusters),
            "duplicate_rows": dup_rows, "duration_s": duration,
            "report_path": str(REPORT_PATH),
        }
    finally:
        db.close()


def run_async() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, phase="start", done=0, total=0,
                      error=None, finished_at=None)

    def _worker():
        try:
            run()
        except Exception as exc:  # noqa: BLE001 – vlákno nesmí umřít potichu
            log.error("audit surovin selhal: %s\n%s", exc, traceback.format_exc())
            _set(error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _set(running=False, phase=None, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="ingredient-audit").start()
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run(), ensure_ascii=False, indent=2))
