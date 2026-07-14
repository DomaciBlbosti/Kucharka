"""AI plánovač jídelníčku.

Sestavuje plán z EXISTUJÍCÍCH receptů (reálné kcal, suroviny, napárování).
Pracuje den po dni: model dostane kompaktní seznam kandidátů a pro každý chod
vybere vhodný recept + 1–2 varianty, s ohledem na pestrost a denní kcal cíl.
Není to lékařské ani dietní doporučení – uživatel si zadá cíl a preference.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe, RecipeIngredient
from . import rag
from .ollamachat import chat_json

log = logging.getLogger("kucharka.planner")

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "day": 0,
    "days": 0,
    "proposal": None,
    "error": None,
    "finished_at": None,
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        return dict(_state)


def _candidates(db, pool: int = 60) -> tuple[list[dict], dict]:
    recipes = db.scalars(
        select(Recipe)
        .options(selectinload(Recipe.ingredients).selectinload(RecipeIngredient.ingredient))
        .order_by(Recipe.rating.desc().nullslast())
        .limit(pool * 2)
    ).all()
    if len(recipes) > pool:
        recipes = random.sample(recipes, pool)
    compact: list[dict] = []
    by_id: dict[int, dict] = {}
    for r in recipes:
        ings = []
        for ri in r.ingredients[:4]:
            ings.append(ri.ingredient.name_cs if ri.ingredient else ri.raw_text)
        item = {
            "id": r.id,
            "title": r.title,
            "kcal": round(r.kcal_per_serving) if r.kcal_per_serving else None,
            "suroviny": ", ".join(ings),
        }
        compact.append(item)
        by_id[r.id] = {"recipe_id": r.id, "title": r.title, "kcal_per_serving": r.kcal_per_serving}
    return compact, by_id


def _pick_day(cands, meals, daily_kcal, preferences, used, day_idx) -> dict:
    meal_list = ", ".join(meals)
    pref = preferences.strip() or "žádné speciální"
    if daily_kcal:
        kcal_line = f"Cílový denní příjem ~{daily_kcal} kcal, rozlož ho mezi chody."
    else:
        kcal_line = "Bez konkrétního kalorického cíle, drž rozumné porce."
    used_line = ", ".join(str(u) for u in list(used)[:50]) or "žádné"
    prompt = (
        f"Jsi výživový rádce. Sestav jídla na JEDEN den (den {day_idx}). "
        f"Chody: {meal_list}. {kcal_line} "
        f"Preference uživatele: {pref}. "
        "Vybírej POUZE recepty ze seznamu níže podle jejich id. Ke každému chodu "
        "vyber recept, který se pro daný chod hodí (snídaně=snídaňové apod.), a 1–2 "
        "alternativy. Upřednostni pestrost; nepoužívej už použitá id: "
        f"{used_line}. Odpověz POUZE JSON ve tvaru "
        '{"meals": {"<chod>": {"recipe_id": <id>, "alternatives": [<id>, ...]}}}.\n'
        f"Recepty: {json.dumps(cands, ensure_ascii=False)}"
    )
    out = chat_json(
        settings.ollama_url,
        settings.ollama_model,
        prompt,
        timeout=max(settings.http_timeout, 180),
        temperature=0.4,
    )
    if out is None:
        raise RuntimeError("plánovač: volání modelu selhalo nebo odpověď nešla naparsovat")
    return out


def _generate_for_slot(db, meal: str, daily_kcal, preferences: str, meals_count: int) -> dict | None:
    """Když z knihovny nic nesedí, vygeneruj a rovnou ulož nový recept (RAG,
    stejná cesta jako u ručního „Vymyslet")."""
    per_meal_kcal = round(daily_kcal / meals_count) if daily_kcal and meals_count else None
    prompt = meal
    if preferences.strip():
        prompt += f", preference: {preferences.strip()}"
    try:
        gen = rag.generate(db, prompt, max_kcal=per_meal_kcal)
        recipe = rag.save_generated(db, gen["recipe"])
        return {
            "recipe_id": recipe.id,
            "title": recipe.title,
            "kcal_per_serving": recipe.kcal_per_serving,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("dogenerování receptu (%s) selhalo: %s", meal, exc)
        return None


def suggest(
    start: date,
    days: int,
    meals: list[str],
    daily_kcal,
    preferences: str,
    fill_empty: bool = False,
) -> None:
    _set(running=True, day=0, days=days, proposal=None, error=None, finished_at=None)
    db = SessionLocal()
    try:
        cands, by_id = _candidates(db)
        if not cands:
            _set(error="Žádné recepty k plánování – přidej nebo nech stáhnout recepty.")
            return
        used: set[int] = set()
        plan = []
        for i in range(days):
            d = start + timedelta(days=i)
            try:
                out = _pick_day(cands, meals, daily_kcal, preferences, used, i + 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("plán den %s selhal: %s", i + 1, exc)
                out = {"meals": {}}
            day_obj = {"date": d.isoformat(), "meals": {}}
            for meal in meals:
                slot = (out.get("meals") or {}).get(meal) or {}
                rid = slot.get("recipe_id")
                alts = [a for a in (slot.get("alternatives") or []) if a in by_id and a != rid][:2]
                if rid in by_id:
                    used.add(rid)
                    day_obj["meals"][meal] = {"recipe_id": rid, "alternatives": alts}
                elif fill_empty:
                    gen = _generate_for_slot(db, meal, daily_kcal, preferences, len(meals))
                    if gen:
                        by_id[gen["recipe_id"]] = {**gen, "generated": True}
                        used.add(gen["recipe_id"])
                        day_obj["meals"][meal] = {"recipe_id": gen["recipe_id"], "alternatives": []}
                    else:
                        day_obj["meals"][meal] = None
                else:
                    day_obj["meals"][meal] = None
            plan.append(day_obj)
            _set(day=i + 1)
        _set(proposal={"start": start.isoformat(), "meals": meals, "recipes": by_id, "days": plan})
    finally:
        db.close()
        _set(running=False, finished_at=time.time())


def suggest_async(start, days, meals, daily_kcal, preferences, fill_empty=False) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    threading.Thread(
        target=suggest, args=(start, days, meals, daily_kcal, preferences, fill_empty), daemon=True
    ).start()
    return True
