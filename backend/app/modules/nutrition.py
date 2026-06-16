"""Výpočet hmotnosti a kalorií.

Převod jednotek na gramy: objemové jednotky → ml → gramy přes hustotu suroviny
(default 1.0 = jako voda). Pro kusové/lžícové jednotky používáme hrubé odhady,
které jdou kdykoli zpřesnit per-surovina.
"""
from __future__ import annotations

from ..models import Ingredient, Recipe

# Objemové jednotky → ml
UNIT_TO_ML: dict[str, float] = {
    "ml": 1.0,
    "dl": 100.0,
    "l": 1000.0,
    "lžička": 5.0,
    "lzicka": 5.0,
    "lžičky": 5.0,
    "lzicky": 5.0,
    "čl": 5.0,
    "lžíce": 15.0,
    "lzice": 15.0,
    "pl": 15.0,
    "hrnek": 250.0,
    "hrnky": 250.0,
    "hrnků": 250.0,
    "sklenice": 250.0,
    "šálek": 200.0,
    "salek": 200.0,
}

# Jednotky, které jsou rovnou v gramech
UNIT_TO_G: dict[str, float] = {
    "g": 1.0,
    "gram": 1.0,
    "gramů": 1.0,
    "dkg": 10.0,
    "deka": 10.0,
    "kg": 1000.0,
}

# Hrubé hmotnosti kusových jednotek (g), když nemáme nic lepšího
PIECE_GRAMS: dict[str, float] = {
    "ks": 60.0,
    "kus": 60.0,
    "kusů": 60.0,
    "plátek": 20.0,
    "platek": 20.0,
    "plátky": 20.0,
    "platky": 20.0,
    "stroužek": 5.0,
    "strouzek": 5.0,
    "stroužky": 5.0,
    "strouzky": 5.0,
    "špetka": 0.5,
    "spetka": 0.5,
    "hrst": 30.0,
    "hrsti": 30.0,
    "snítka": 2.0,
    "snitka": 2.0,
    "snítky": 2.0,
    "snitky": 2.0,
    "konzerva": 400.0,
    "konzervy": 400.0,
    "balení": 250.0,
    "baleni": 250.0,
}


def grams_for(
    amount: float | None, unit: str | None, ingredient: Ingredient | None
) -> float | None:
    """Vrať hmotnost v gramech, nebo None když to nejde spočítat."""
    if amount is None:
        return None
    u = (unit or "").strip().lower()

    if u in UNIT_TO_G:
        return amount * UNIT_TO_G[u]

    if u in UNIT_TO_ML:
        density = (ingredient.density if ingredient and ingredient.density else 1.0)
        return amount * UNIT_TO_ML[u] * density

    if u in PIECE_GRAMS:
        return amount * PIECE_GRAMS[u]

    # Bez jednotky bereme číslo jako počet kusů ~ rozumný default
    if u == "":
        return amount * 60.0

    return None


def kcal_for(grams: float | None, ingredient: Ingredient | None) -> float | None:
    if grams is None or ingredient is None or ingredient.kcal_100g is None:
        return None
    return round(grams / 100.0 * ingredient.kcal_100g, 1)


def recompute_recipe_kcal(recipe: Recipe) -> None:
    """Přepočítej kcal/porce z navázaných ingrediencí (in-place)."""
    total = 0.0
    have_any = False
    for ri in recipe.ingredients:
        if ri.kcal is not None:
            total += ri.kcal
            have_any = True
    if not have_any:
        recipe.kcal_per_serving = None
        return
    servings = recipe.servings or 1
    recipe.kcal_per_serving = round(total / max(servings, 1), 0)
