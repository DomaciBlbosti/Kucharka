"""Kontrola dietních tagů proti surovinám receptu.

Proč: tagy přiděluje model a nikdo jeho tvrzení neověřoval, takže recept
„Kořeněné mleté maso s kuskusem" vyšel jako *Vegetariánské*. Model navíc
vidí jen prvních osm surovin (viz tagging), takže maso na devátém řádku
nemá jak poznat.

Tenhle modul umí říct, které dietní tagy jsou se surovinami v rozporu.
Používá se dvakrát: při přidělování tagů (rozporné se zahodí) a jako
jednorázový úklid už uložených tagů.

Rozhoduje se ve dvou krocích:

  1. NAPÁROVANÁ surovina – rozhoduje kategorie z číselníku (modules/taxonomy).
     To je tvrdý údaj: `maso > cokoli` znamená, že recept není vegetariánský.
  2. NENAPÁROVANÁ surovina a název receptu – klíčová slova přes stemmer,
     takže „kuře"/„kuřecí"/„kuřete" chytne stejně. Bez tohohle kroku by
     kontrola u receptu s nenapárovanými surovinami neuměla nic – a to je
     zrovna ten případ, kde se model plete nejčastěji.

Kontrola umí jen VYVRACET, ne potvrzovat: chybějící maso mezi surovinami
neznamená, že recept vegetariánský je (může to být nenapárovaný „bujón").
Proto se tagy jen odebírají, nikdy nedoplňují.
"""
from __future__ import annotations

import logging

from . import textnorm

log = logging.getLogger("kucharka.diet")

VEGETARIAN = "vegetarianske"
VEGAN = "veganske"

# Top kategorie z číselníku, které dietu vylučují.
_MEAT_TOPS = {"maso", "ryby a mořské plody"}
_ANIMAL_TOPS = {"mléčné výrobky", "vejce"}

# Výjimky uvnitř živočišného topu: sójové a ovesné „mléko" se v číselníku
# vede pod mléčnými výrobky (tam ho lidi hledají), veganské ale nevylučuje.
_VEGAN_OK_PATHS = {"mléčné výrobky > rostlinné alternativy"}

# Kmeny (viz textnorm.stem_word) pro nenapárované řádky a název receptu.
# Maso a ryby – vylučují vegetariánské I veganské.
_MEAT_STEMS = frozenset("""
mas kur kurc krut vepr hovez telc jehnec kralik zverin kachn hus bazant
slanin sunk klobas spek sadl anglick
ryb los tunak tresk pstruh sardink ancovick kapr makrel sled uzenac
krevet krab chobotnik musl humr kalamar sepi ustric
zelatin
""".split())

# „sal" (salám) a „park" (párek) jsou krátké kmeny a mimo kuchyň by braly
# i nesouvisející slova; v seznamu surovin je riziko zanedbatelné, ale
# schválně jsou oddělené, aby bylo vidět, že jde o vědomý ústupek.
_MEAT_STEMS = _MEAT_STEMS | {"sal", "park"}

# Živočišné, ale vegetariánské – vylučují jen veganské.
_ANIMAL_STEMS = frozenset("""
mlek smetan masl syr tvaroh jogurt vejc zloutek bilek med majonez
""".split())

# Rostlinné náhražky: „kokosové mléko" je veganské, i když obsahuje „mléko".
# Když je v řádku některý z těchto kmenů, živočišný kmen se ignoruje.
_PLANT_STEMS = frozenset("""
kokos soj sojov mandl ovesn ryzov konopn kesu rostlinn vegan tof
slunecnicov lisk spaldov pohank
""".split())


def _row_conflicts(text: str) -> tuple[bool, bool]:
    """(je tam maso/ryba, je tam živočišná surovina) podle textu řádku."""
    stems = set(textnorm.tokens(text or ""))
    if not stems:
        return False, False
    meat = bool(stems & _MEAT_STEMS)
    animal = bool(stems & _ANIMAL_STEMS) and not (stems & _PLANT_STEMS)
    return meat, animal


def conflicts(title: str | None, ingredient_rows) -> set[str]:
    """Které dietní tagy recept mít NESMÍ.

    `ingredient_rows` jsou RecipeIngredient (nebo cokoli s `raw_text`
    a `ingredient`). Vrací podmnožinu {VEGETARIAN, VEGAN}; veganský tag padá
    vždycky, když padá vegetariánský.
    """
    meat = animal = False

    for ri in ingredient_rows or []:
        if getattr(ri, "nonfood", False):
            continue  # alobal a spol. o dietě nic neříkají
        ing = getattr(ri, "ingredient", None)
        path = getattr(ing, "category_path", None) if ing is not None else None
        if path:
            path = str(path).strip()
            top = path.split(">")[0].strip()
            if top in _MEAT_TOPS:
                meat = True
            elif top in _ANIMAL_TOPS and path not in _VEGAN_OK_PATHS:
                animal = True
            # Napárovaná surovina s kategorií je tvrdý údaj – text už neřešíme.
            continue
        m, a = _row_conflicts(getattr(ri, "raw_text", "") or "")
        meat = meat or m
        animal = animal or a

    # Název receptu jako záchrana: „Kořeněné mleté maso s kuskusem" se pozná,
    # i kdyby suroviny chyběly nebo byly všechny nenapárované.
    m, _a = _row_conflicts(title or "")
    meat = meat or m

    out: set[str] = set()
    if meat:
        out |= {VEGETARIAN, VEGAN}
    elif animal:
        out.add(VEGAN)
    return out


def allowed_tag_keys(title: str | None, ingredient_rows, keys) -> set[str]:
    """Vyfiltruj z navržených tagů ty, které surovinám odporují.

    `keys` jsou klíče ve tvaru 'jmenny_prostor:slug' (viz tagging).
    """
    bad = conflicts(title, ingredient_rows)
    if not bad:
        return set(keys)
    return {k for k in keys if k.split(":", 1)[-1] not in bad}
