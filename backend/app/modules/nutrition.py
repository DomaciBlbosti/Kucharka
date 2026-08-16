"""Výpočet hmotnosti a kalorií.

Převod jednotek na gramy: objemové jednotky → ml → gramy přes hustotu suroviny
(default 1.0 = jako voda). Pro kusové/lžícové jednotky používáme hrubé odhady,
které jdou kdykoli zpřesnit per-surovina.

Rozpoznávání jednotek v textu je tady taky (canonical_unit / find_unit), ať
oba regex parsery (normalizer, enrichment) sdílejí jedno chování: skloňované
tvary („3 lžic"), přívlastky („1 čajová lžička") i anglické jednotky (tbsp).
Bez toho parser jednotku nepoznal, spadl na default „číslo × 60 g" a lžíce
oleje pak měla přes 500 kcal.
"""
from __future__ import annotations

import unicodedata

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
    "tsp": 5.0,
    "lžíce": 15.0,
    "lzice": 15.0,
    "pl": 15.0,
    "tbsp": 15.0,
    "hrnek": 250.0,
    "hrnky": 250.0,
    "hrnků": 250.0,
    "cup": 240.0,
    "cups": 240.0,
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
    "oz": 28.35,
    "lb": 453.6,
    "lbs": 453.6,
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


# ─── Rozpoznání jednotky v textu ─────────────────────────────────────────────

def _strip_acc(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


# Skloňované tvary → kanonická jednotka (klíče bez diakritiky, lowercase).
# Kanonická hodnota MUSÍ být klíčem některé tabulky výš.
_INFLECTED_UNITS: dict[str, str] = {
    # lžíce (15 ml)
    "lzic": "lžíce", "lzici": "lžíce", "lzicemi": "lžíce", "lzicich": "lžíce",
    # lžička (5 ml)
    "lzicek": "lžička", "lzicce": "lžička", "lzicku": "lžička", "lzickou": "lžička",
    "lzickami": "lžička",
    # hrnek / šálek / sklenice
    "hrnku": "hrnek", "hrncich": "hrnek",
    "salku": "šálek", "salky": "šálek",
    "sklenici": "sklenice", "sklenic": "sklenice",
    # kusové
    "kusy": "ks", "kousek": "ks", "kousky": "ks",
    "strouzku": "stroužek", "platku": "plátek",
    "spetky": "špetka", "spetku": "špetka",
    "snitek": "snítka", "snitku": "snítka",
    "konzerv": "konzerva", "konzervu": "konzerva",
    "balicek": "balení", "balicku": "balení",
}

_UNIT_LOOKUP: dict[str, str] = {}
for _k in (*UNIT_TO_G, *UNIT_TO_ML, *PIECE_GRAMS):
    _UNIT_LOOKUP.setdefault(_strip_acc(_k), _k)
for _infl, _canon in _INFLECTED_UNITS.items():
    _UNIT_LOOKUP.setdefault(_infl, _canon)

# Přívlastky před jednotkou („1 ČAJOVÁ lžička", „2 POLÉVKOVÉ lžíce") – samy
# o sobě jednotka nejsou, velikost nese podstatné jméno za nimi.
UNIT_QUALIFIERS = frozenset({
    "cajova", "cajove", "cajovou", "kavova", "kavove", "kavovou",
    "polevkova", "polevkove", "polevkovou", "dezertni",
    "vrchovata", "vrchovate", "vrchovatou", "zarovnana", "zarovnane", "zarovnanou",
    "velka", "velke", "velkou", "mala", "male", "malou",
    "heaping", "heaped", "level",
})


def canonical_unit(token: str) -> str | None:
    """Kanonická jednotka pro token („lžic" → „lžíce"), nebo None."""
    return _UNIT_LOOKUP.get(_strip_acc((token or "").lower().strip(",.;")))


def find_unit(tokens: list[str], max_skip: int = 2) -> tuple[str | None, int]:
    """Najdi jednotku na začátku tokenů (hned za číslem). Přeskočí až
    `max_skip` přívlastků („čajová lžička"). Vrací (kanonická jednotka,
    počet spotřebovaných tokenů) – přívlastky se počítají jen při nálezu."""
    idx = 0
    while (
        idx < len(tokens) and idx < max_skip
        and _strip_acc(tokens[idx].lower()) in UNIT_QUALIFIERS
    ):
        idx += 1
    if idx < len(tokens):
        u = canonical_unit(tokens[idx])
        if u:
            return u, idx + 1
    return None, 0


def grams_for(
    amount: float | None, unit: str | None, ingredient: Ingredient | None
) -> float | None:
    """Vrať hmotnost v gramech, nebo None když to nejde spočítat."""
    if amount is None:
        return None
    u = (unit or "").strip().lower()
    if u and u not in UNIT_TO_G and u not in UNIT_TO_ML and u not in PIECE_GRAMS:
        # historicky uložené skloňované tvary („lžic") – zkus kanonizaci
        u = canonical_unit(u) or u

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
    """Přepočítej kcal/porce z navázaných ingrediencí (in-place).

    Zároveň udržuje denormalizovaný `ing_total` (počet napárovaných řádků)
    pro rychlý výpis – tahle funkce se volá přesně tam, kde se řádky surovin
    mění (ingest, enrichment, backfill, llm_match, překlad)."""
    recipe.ing_total = sum(1 for ri in recipe.ingredients if ri.ingredient_id is not None)
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
