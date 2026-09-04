"""Slučování duplicitních surovin.

Audit slovníku (`ingredient_audit`) našel v produkci 1 473 shluků a 2 894
nadbytečných řádků – čtvrtinu slovníku. Nejhorší třída jsou DOSLOVA shodné
názvy: 98× „máslo", 81× „mléko", 41× „česnek". Vznikly cestou, která před
zápisem nic nedohledávala (viz `ingredient_resolve`); ta je zavřená, ale
historii je potřeba uklidit.

Sloučení je destruktivní, proto se řeší ve dvou režimech:

  * `merge_exact_names()` – dávkově, ale JEN pro shluky s naprosto shodným
    názvem. Tam není co posuzovat: „máslo" a „máslo" je jedna surovina.
  * `merge_cluster()` – ruční sloučení konkrétních id, pro ostatní třídy
    z auditu. Ty se automaticky slučovat NESMÍ; export ukazuje proč –
    „Těsto"/„tresti", „plátek másla"/„plátky masa", „Vepřové maso
    (krkovice)"/„uzené maso (krkovice)" nebo záměrně rozlišené „Čokoláda
    hořká 45-59 %" vs „60-69 %".

Vítěz shluku: referenční záznam z NutriDatabáze, jinak ten s nejvíc
recepty, při shodě nejnižší id (nejstarší). Poražení se přepojí a smažou.

Přepojit je potřeba VŠECHNY vazby na surovinu, každou s vlastním úskalím:
  recipe_ingredient  – prostý UPDATE, ale řádkům se musí přepočítat gramáž
                       a kcal: vítěz může mít jinou výživu (máslo 753 vs 719)
  pantry_item        – UNIQUE(ingredient_id): když je vítěz ve spíži taky,
                       řádek poraženého se maže, ne přepojuje
  ingredient_embedding – ingredient_id je primární klíč, stejný případ
  ingredient_alias   – alias i lookup_key jsou globálně unikátní → přepojení
                       nemůže kolidovat
  match_decision, shopping_item, barcode_map – prostý UPDATE
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import (
    BarcodeMap,
    Ingredient,
    IngredientAlias,
    IngredientEmbedding,
    MatchDecision,
    PantryItem,
    RecipeIngredient,
    ShoppingItem,
)
from .ingredient_resolve import NUTRIDB_SOURCE
from .ingredient_resolve import invalidate as invalidate_resolver

log = logging.getLogger("kucharka.ingredient_merge")

_lock = threading.Lock()
_state: dict = {
    "running": False, "dry_run": True, "done": 0, "total": 0,
    "merged_clusters": 0, "removed_ingredients": 0, "moved_rows": 0,
    "recipes_touched": 0, "error": None,
    "started_at": None, "finished_at": None, "duration_s": None,
}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        return dict(_state)


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


# ─── Hledání shluků se shodným názvem ────────────────────────────────────────

def exact_name_clusters(db: Session) -> list[tuple[int, list[int]]]:
    """[(vítěz, [poražení…])] pro každý název, který má víc než jeden záznam.

    Porovnává se `lower(trim(name_cs))` – přesně ta třída, kterou audit
    označuje jako „shodný název" a u které není co posuzovat.
    """
    rows = db.execute(
        select(Ingredient.id, Ingredient.name_cs, Ingredient.source)
    ).all()
    recipes = dict(db.execute(
        select(RecipeIngredient.ingredient_id, func.count())
        .where(RecipeIngredient.ingredient_id.is_not(None))
        .group_by(RecipeIngredient.ingredient_id)
    ).all())

    by_name: dict[str, list[tuple]] = {}
    for r in rows:
        if not r.name_cs:
            continue
        by_name.setdefault(r.name_cs.strip().lower(), []).append(r)

    out: list[tuple[int, list[int]]] = []
    for members in by_name.values():
        if len(members) < 2:
            continue
        ranked = sorted(
            members,
            key=lambda m: (m.source != NUTRIDB_SOURCE, -recipes.get(m.id, 0), m.id),
        )
        out.append((ranked[0].id, [m.id for m in ranked[1:]]))
    return out


# ─── Vlastní sloučení ────────────────────────────────────────────────────────

def merge_cluster(db: Session, keep_id: int, loser_ids: list[int]) -> dict:
    """Přepoj všechny vazby poražených na vítěze a poražené smaž.

    Necommituje – o transakci rozhoduje volající (dávka commituje po
    shlucích, ať je běh přerušitelný). Vrací statistiku a dotčené recepty.
    """
    losers = [i for i in loser_ids if i != keep_id]
    keep = db.get(Ingredient, keep_id)
    if keep is None or not losers:
        return {"moved_rows": 0, "removed": 0, "recipe_ids": set()}

    from .nutrition import grams_for, kcal_for

    # 1) řádky receptů – přepojit A přepočítat, vítěz může mít jinou výživu
    rows = db.scalars(
        select(RecipeIngredient).where(RecipeIngredient.ingredient_id.in_(losers))
    ).all()
    recipe_ids = set()
    for r in rows:
        r.ingredient_id = keep_id
        r.grams = grams_for(r.amount, r.unit, keep)
        r.kcal = kcal_for(r.grams, keep)
        recipe_ids.add(r.recipe_id)

    # 2) spíž – UNIQUE(ingredient_id): pokud vítěz ve spíži už je, řádek
    #    poraženého zahodíme (množství nesčítáme, jednotky nemusí sedět)
    keep_in_pantry = db.scalar(
        select(func.count()).select_from(PantryItem)
        .where(PantryItem.ingredient_id == keep_id)
    ) or 0
    pantry = db.scalars(
        select(PantryItem).where(PantryItem.ingredient_id.in_(losers))
    ).all()
    for i, item in enumerate(pantry):
        if keep_in_pantry or i > 0:
            db.delete(item)
        else:
            item.ingredient_id = keep_id

    # 3) embeddingy – ingredient_id je primární klíč, poražené se jen smažou
    #    (index se dopočítá při nejbližším reindexu)
    db.execute(
        delete(IngredientEmbedding)
        .where(IngredientEmbedding.ingredient_id.in_(losers))
    )

    # 4) zbytek jde prostým UPDATE – alias i lookup_key jsou globálně
    #    unikátní, takže přepojení nemůže kolidovat
    for model in (IngredientAlias, MatchDecision, ShoppingItem, BarcodeMap):
        db.execute(
            update(model)
            .where(model.ingredient_id.in_(losers))
            .values(ingredient_id=keep_id)
        )

    # Session má autoflush=False (viz db.py), takže změny v ORM objektech výš
    # (přepojené řádky receptů, smazané položky spíže) v tuhle chvíli ještě
    # NEJSOU v databázi. Bez tohohle flushe by DELETE níž narazil na cizí klíč
    # `recipe_ingredient_ibfk_2` – řádky by pořád ukazovaly na poražené id.
    db.flush()
    db.execute(delete(Ingredient).where(Ingredient.id.in_(losers)))
    return {"moved_rows": len(rows), "removed": len(losers), "recipe_ids": recipe_ids}


def _recompute(db: Session, recipe_ids: set[int]) -> None:
    """Přepočítej kalorie receptů, kterých se sloučení dotklo."""
    from ..models import Recipe
    from .nutrition import recompute_recipe_kcal

    ids = list(recipe_ids)
    for i in range(0, len(ids), 500):
        for r in db.scalars(
            select(Recipe).where(Recipe.id.in_(ids[i : i + 500]))
        ).all():
            recompute_recipe_kcal(r)
        db.commit()


def merge_exact_names(dry_run: bool = True) -> dict:
    """Sloučí VŠECHNY shluky se shodným názvem. `dry_run` jen spočítá."""
    started = time.monotonic()
    db = SessionLocal()
    try:
        clusters = exact_name_clusters(db)
        _set(total=len(clusters), done=0, merged_clusters=0,
             removed_ingredients=0, moved_rows=0, recipes_touched=0,
             dry_run=dry_run, error=None)
        log.info("Slučování surovin (%s): %s shluků se shodným názvem",
                 "nanečisto" if dry_run else "naostro", len(clusters))

        moved = removed = 0
        touched: set[int] = set()
        for i, (keep_id, losers) in enumerate(clusters, 1):
            if dry_run:
                removed += len(losers)
                moved += db.scalar(
                    select(func.count()).select_from(RecipeIngredient)
                    .where(RecipeIngredient.ingredient_id.in_(losers))
                ) or 0
            else:
                out = merge_cluster(db, keep_id, losers)
                db.commit()
                moved += out["moved_rows"]
                removed += out["removed"]
                touched |= out["recipe_ids"]
            _set(done=i, merged_clusters=i, removed_ingredients=removed,
                 moved_rows=moved, recipes_touched=len(touched))

        if not dry_run:
            invalidate_resolver()
            _set(error=None)
            log.info("Slučování: přepočítávám kalorie %s receptů", len(touched))
            _recompute(db, touched)

        duration = round(time.monotonic() - started, 1)
        _set(duration_s=duration)
        out = {
            "dry_run": dry_run,
            "clusters": len(clusters),
            "removed_ingredients": removed,
            "moved_rows": moved,
            "recipes_touched": len(touched),
            "duration_s": duration,
        }
        log.info("Slučování hotové: %s", out)
        return out
    finally:
        db.close()


def merge_exact_names_async(dry_run: bool = True) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started_at=time.time(), finished_at=None,
                      error=None, dry_run=dry_run)

    def _worker():
        try:
            merge_exact_names(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 – vlákno nesmí umřít potichu
            log.error("slučování surovin selhalo: %s\n%s", exc, traceback.format_exc())
            _set(error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _set(running=False, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="ingredient-merge").start()
    return True


def merge_manual(keep_id: int, loser_ids: list[int]) -> dict:
    """Ruční sloučení konkrétního shluku (ostatní třídy z auditu)."""
    db = SessionLocal()
    try:
        keep = db.get(Ingredient, keep_id)
        if keep is None:
            raise ValueError(f"surovina id={keep_id} neexistuje")
        out = merge_cluster(db, keep_id, loser_ids)
        db.commit()
        invalidate_resolver()
        _recompute(db, out["recipe_ids"])
        log.info("Ruční sloučení do id=%s (%r): %s surovin, %s řádků",
                 keep_id, keep.name_cs, out["removed"], out["moved_rows"])
        return {
            "keep_id": keep_id,
            "keep_name": keep.name_cs,
            "removed_ingredients": out["removed"],
            "moved_rows": out["moved_rows"],
            "recipes_touched": len(out["recipe_ids"]),
            "at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        db.close()
