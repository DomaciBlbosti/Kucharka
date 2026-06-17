"""Import exportu z NutriDatabaze.cz do tabulky `ingredient`.

Export si stáhneš po (bezplatné) registraci na nutridatabaze.cz. Aktuální
formát (v9): CSV v kódování cp1250, oddělovač ';', sloupce OrigFdNm / EngFdNam /
OrigFdCd / ENERC [kcal] / PROT [g] / CHO [g] / FAT [g] / FIBT [g] …

Co dělá:
  * existující surovinu (i ollama-odhad) podle kódu nebo názvu ZPŘESNÍ reálnými
    daty a označí source="nutridatabaze",
  * neznámé potraviny přidá jako novou referenci,
  * doplní aliasy (český i anglický název), ať se recepty líp párují,
  * přepočítá kalorie dotčených receptů.

Volitelně --merge-ollama: ollama-suroviny, které mají přesný protějšek
v NutriDatabaze, sloučí (přepojí recepty/spíž/nákup, duplicitu smaže).

Použití:
    python -m app.seed.import_nutridb /cesta/export.csv [--merge-ollama] [--no-recompute]
"""
from __future__ import annotations

import argparse
import csv
import sys

from sqlalchemy import select, update

from ..db import Base, SessionLocal, engine
from ..models import (
    Ingredient,
    IngredientAlias,
    PantryItem,
    Recipe,
    RecipeIngredient,
    ShoppingItem,
)
from ..modules.normalizer import _clean_name, _norm
from ..modules.nutrition import kcal_for, recompute_recipe_kcal

# normalizované názvy sloupců → pole modelu
COLMAP = {
    "origfdnm": "name", "engfdnam": "name_en", "origfdcd": "code",
    "enerc [kcal]": "kcal", "prot [g]": "protein", "cho [g]": "carbs",
    "fat [g]": "fat", "fibt [g]": "fiber",
}


def _load_rows(path: str) -> list[dict]:
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "cp1250", "iso-8859-2"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise SystemExit("Nepodařilo se dekódovat soubor (zkoušel utf-8/cp1250/latin2).")
    sample = text[:4096]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delim, skipinitialspace=True))


def _num(val) -> float | None:
    if val in (None, ""):
        return None
    try:
        return float(str(val).strip().replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


def _resolve_cols(header: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in header:
        key = COLMAP.get(_norm(h))
        if key:
            out[key] = h
    return out


def _add_alias(db, seen: set[str], alias: str, ing_id: int) -> None:
    key = _clean_name(alias)
    if not key or key in seen:
        return
    if db.scalar(select(IngredientAlias).where(IngredientAlias.alias == key)):
        seen.add(key)
        return
    db.add(IngredientAlias(alias=key, ingredient_id=ing_id))
    seen.add(key)


def import_csv(db, path: str) -> tuple[int, int]:
    rows = _load_rows(path)
    if not rows:
        raise SystemExit("Prázdný soubor.")
    cols = _resolve_cols(list(rows[0].keys()))
    if "name" not in cols:
        raise SystemExit(f"Nenašel jsem sloupec s názvem. Sloupce: {list(rows[0].keys())}")
    print(f"Mapování sloupců: {cols}")

    # indexy existujících surovin
    all_ings = db.scalars(select(Ingredient)).all()
    by_code = {i.code: i for i in all_ings if i.code}
    by_name = {}
    for i in all_ings:
        by_name.setdefault(_norm(i.name_cs), i)
    seen_alias = {a for (a,) in db.execute(select(IngredientAlias.alias)).all()}

    inserted = enriched = 0
    for row in rows:
        name = (row.get(cols["name"]) or "").strip()
        if not name:
            continue
        code = (row.get(cols.get("code", "")) or "").strip() or None
        name_en = (row.get(cols.get("name_en", "")) or "").strip() or None

        ing = (by_code.get(code) if code else None) or by_name.get(_norm(name))
        new = ing is None
        if new:
            ing = Ingredient(name_cs=name)
            db.add(ing)
        ing.name_en = name_en or ing.name_en
        ing.code = code or ing.code
        ing.kcal_100g = _num(row.get(cols.get("kcal", "")))
        ing.protein_100g = _num(row.get(cols.get("protein", "")))
        ing.carbs_100g = _num(row.get(cols.get("carbs", "")))
        ing.fat_100g = _num(row.get(cols.get("fat", "")))
        ing.fiber_100g = _num(row.get(cols.get("fiber", "")))
        ing.source = "nutridatabaze"
        db.flush()  # potřebujeme id pro aliasy

        by_code[code] = ing if code else by_code.get(code)
        by_name.setdefault(_norm(name), ing)
        _add_alias(db, seen_alias, name, ing.id)
        if name_en:
            _add_alias(db, seen_alias, name_en, ing.id)

        if new:
            inserted += 1
        else:
            enriched += 1
    db.commit()
    return inserted, enriched


def merge_ollama(db, cutoff: int = 90) -> int:
    """Slouč ollama-suroviny s přesným protějškem v NutriDatabaze."""
    from rapidfuzz import fuzz, process

    nutri = db.scalars(
        select(Ingredient).where(Ingredient.source == "nutridatabaze")
    ).all()
    if not nutri:
        return 0
    choices = {i.id: _norm(i.name_cs) for i in nutri}
    ollama = db.scalars(select(Ingredient).where(Ingredient.source == "ollama")).all()
    merged = 0
    for o in ollama:
        best = process.extractOne(
            _clean_name(o.name_cs), choices, scorer=fuzz.token_set_ratio,
            score_cutoff=cutoff,
        )
        if not best:
            continue
        target_id = best[2]
        if target_id == o.id:
            continue
        # přepoj FK z ollama → nutridatabaze
        for model in (RecipeIngredient, PantryItem, ShoppingItem, IngredientAlias):
            db.execute(
                update(model).where(model.ingredient_id == o.id)
                .values(ingredient_id=target_id)
            )
        db.delete(o)
        merged += 1
    db.commit()
    return merged


def recompute_all(db) -> int:
    """Přepočítej kalorie všech receptů (po změně výživy surovin)."""
    recipes = db.scalars(select(Recipe)).all()
    for r in recipes:
        for ri in r.ingredients:
            ri.kcal = kcal_for(ri.grams, ri.ingredient)
        recompute_recipe_kcal(r)
    db.commit()
    return len(recipes)


def main() -> int:
    ap = argparse.ArgumentParser(description="Import NutriDatabaze.cz exportu")
    ap.add_argument("path")
    ap.add_argument("--merge-ollama", action="store_true",
                    help="slouč ollama-suroviny s protějškem v NutriDatabaze")
    ap.add_argument("--no-recompute", action="store_true",
                    help="nepřepočítávat kalorie receptů")
    args = ap.parse_args()

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        ins, enr = import_csv(db, args.path)
        print(f"Import: {ins} nových, {enr} zpřesněných surovin.")
        if args.merge_ollama:
            m = merge_ollama(db)
            print(f"Sloučeno ollama-duplikátů: {m}")
        if not args.no_recompute:
            n = recompute_all(db)
            print(f"Přepočítáno kalorií u {n} receptů.")
    finally:
        db.close()
    print("Hotovo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
