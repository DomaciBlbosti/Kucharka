"""Testy rozpoznávání jednotek a přepočtu gramáže/kcal.

Regresní scénář z produkce: „3 lžic olivový olej" – parser nepoznal skloňovaný
tvar → jednotka None → default „číslo × 60 g" → 180 g oleje = 1591 kcal.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.modules.enrichment import _parse_amount_unit  # noqa: E402
from app.modules.normalizer import parse_line_regex  # noqa: E402
from app.modules.nutrition import find_unit, grams_for, kcal_for  # noqa: E402


class FakeIng:
    def __init__(self, kcal_100g=None, density=None):
        self.kcal_100g = kcal_100g
        self.density = density


PASSED = FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}")


def main():
    oil = FakeIng(kcal_100g=884, density=0.92)

    # regresní případ ze screenshotu
    a, u = _parse_amount_unit("3 lžic olivový olej")
    check("'3 lžic' → amount 3", a == 3)
    check("'3 lžic' → jednotka lžíce", u == "lžíce")
    g = grams_for(a, u, oil)
    check("3 lžíce oleje ≈ 41 g (ne 180)", g is not None and 35 <= g <= 50)
    k = kcal_for(g, oil)
    check("3 lžíce oleje < 450 kcal (ne 1591)", k is not None and k < 450)

    a, u = _parse_amount_unit("1 čajová lžička sezamová semínka")
    check("'čajová lžička' → lžička", u == "lžička")
    check("1 čajová lžička = 5 g (hustota 1)", grams_for(a, u, None) == 5.0)

    a, u = _parse_amount_unit("2 polévkové lžíce cukru")
    check("'polévkové lžíce' → lžíce", u == "lžíce")

    a, u = _parse_amount_unit("1 tbsp olive oil")
    check("tbsp → lžíce/15 ml", u is not None and grams_for(a, u, None) == 15.0)

    a, u = _parse_amount_unit("2 tsp paprika")
    check("tsp → 5 ml", u is not None and grams_for(a, u, None) == 10.0)

    a, u = _parse_amount_unit("150 g cukru")
    check("'150 g' beze změny", a == 150 and u == "g" and grams_for(a, u, None) == 150)

    a, u = _parse_amount_unit("2 vejce")
    check("bez jednotky → unit None", u is None)
    check("bez jednotky → kusový default 60 g", grams_for(a, u, None) == 120.0)

    # parse_line_regex: jednotka i přívlastek zmizí ze jména
    a, u, name = parse_line_regex("2 polévkové lžíce hladké mouky")
    check("regex parser: amount 2", a == 2)
    check("regex parser: unit lžíce", u == "lžíce")
    check("regex parser: jméno bez jednotky", name == "hladké mouky")

    a, u, name = parse_line_regex("3 lžic olivového oleje")
    check("regex parser: skloňovaná lžíce", u == "lžíce" and name == "olivového oleje")

    # find_unit: přívlastek bez jednotky nic nespotřebuje
    u, consumed = find_unit(["velká", "cibule"])
    check("'velká cibule' není jednotka", u is None and consumed == 0)

    # kanonizace historicky uložených tvarů přímo v grams_for
    check("grams_for kanonizuje 'lžic'", grams_for(3, "lžic", oil) == grams_for(3, "lžíce", oil))
    check("grams_for: neznámá jednotka → None", grams_for(1, "žejdlík", oil) is None)

    # hustota se aplikuje u objemových jednotek
    check("hustota oleje v přepočtu", grams_for(1, "lžíce", oil) == 15 * 0.92)

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
