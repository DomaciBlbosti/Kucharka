"""Testy číselníku kategorií a kontroly dietních tagů.

Dva problémy z produkce, oba stejného původu – model zapsal metadata a nikdo
je neověřil proti datům:

  1. Kategorie surovin měly pevnou jen první úroveň, podúrovně si model
     dopisoval volným textem. Ve filtru pak byly desítky položek, dvojice
     se stejným významem („přísady“ vs „aditiva“, „kořenová zelenina“ vs
     „kořeninoviny“) a nesmysly z pokaženého překladu („maso > prasine“,
     „ryby a mořské plody > sladkoviny“).
  2. Recept „Kořeněné mleté maso s kuskusem“ byl otagovaný jako Vegetariánské.

Testy hlídají hlavně to, že normalizace radši NEROZHODNE, než aby hádala –
tichá chyba v zařazení je horší než viditelně nezařazená surovina.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-taxonomy-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Ingredient, Recipe, RecipeIngredient  # noqa: E402
from app.modules import categorize, diet, taxonomy  # noqa: E402

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


class FakeIng:
    """Napodobuje RecipeIngredient tam, kde nepotřebujeme databázi."""

    def __init__(self, raw_text="", category_path=None, nonfood=False):
        self.raw_text = raw_text
        self.nonfood = nonfood
        self.ingredient = (
            type("I", (), {"category_path": category_path})() if category_path else None
        )


# ─── Číselník ────────────────────────────────────────────────────────────────

def taxonomy_checks():
    print("\nčíselník:")
    check("má rozumnou velikost (jde projít očima)",
          40 <= len(taxonomy.PATHS) <= 100, str(len(taxonomy.PATHS)))
    check("žádná cesta se neopakuje",
          len(taxonomy.PATHS) == len(set(taxonomy.PATHS)))
    check("nejvýš dvě úrovně",
          all(p.count(">") <= 1 for p in taxonomy.PATHS),
          str([p for p in taxonomy.PATHS if p.count(">") > 1][:3]))
    check("žádná podkategorie se neopakuje v rámci topu",
          all(len(subs) == len(set(subs)) for subs in taxonomy.TAXONOMY.values()))
    check("top bez podkategorií dá cestu jen z topu", "vejce" in taxonomy.PATHS)

    # Zásadní: každý alias musí opravdu vést na svou cestu. Aliasy se píšou
    # s diakritikou a `_key` na ně pouští stemmer – napsat je bez háčků
    # nefunguje („saláty“ → „salat“, ale „salaty“ → „sal“) a alias by se
    # nikdy netrefil, aniž by o tom kdokoli věděl.
    broken = [
        (alias, path) for alias, path in taxonomy._ALIASES.items()
        if taxonomy.normalize_path(f"{path.split(' > ')[0]} > {alias}") != path
    ]
    check("každý alias vede na svou cestu", not broken, str(broken[:5]))
    bad_target = [p for p in taxonomy._ALIASES.values() if p not in taxonomy.PATHS]
    check("každý alias míří na cestu z číselníku", not bad_target, str(bad_target[:5]))

    print("\nnormalizace uložených cest:")
    # Přesně to, co bylo vidět ve filtru na produkci.
    cases = {
        "zelenina > kořenová zelenina": "zelenina > kořenová zelenina",
        "zelenina > kořeninoviny": "zelenina > kořenová zelenina",
        "maso > prasine": "maso > vepřové",
        "ryby a mořské plody > sladkoviny": "ryby a mořské plody > sladkovodní ryby",
        "luštěniny > boby": "luštěniny > fazole",
        "ostatní > octy": "ostatní > ocet",
        "ostatní > aditiva": "ostatní > přídatné látky",
        "mléčné výrobky > sýr": "mléčné výrobky > sýry",
        "nápoje > alkohol": "nápoje > alkoholické nápoje",
        "nápoje > ovocné nápoje": "nápoje > džusy a šťávy",
        "obiloviny a pečivo > sušenky": "obiloviny a pečivo > sušenky a oplatky",
        "ořechy a semínka > kokos": "ořechy a semínka > ořechy",
        "zelenina > saláty": "zelenina > listová zelenina",
    }
    for src, want in cases.items():
        got = taxonomy.normalize_path(src)
        check(f"{src} → {want}", got == want, str(got))

    check("třetí úroveň se zahodí, ne aby cesta propadla",
          taxonomy.normalize_path("maso > drůbež > kuřecí prsa") == "maso > drůbež",
          str(taxonomy.normalize_path("maso > drůbež > kuřecí prsa")))
    check("cesta z číselníku projde beze změny",
          taxonomy.normalize_path("maso > hovězí") == "maso > hovězí")
    check("top bez podkategorií projde sám o sobě",
          taxonomy.normalize_path("vejce") == "vejce")

    print("\nnormalizace radši nerozhodne, než aby hádala:")
    for src in ("sladidla > dezerty", "maso > červené maso", "ostatní > přísady"):
        check(f"{src} → na model", taxonomy.normalize_path(src) is None,
              str(taxonomy.normalize_path(src)))
    check("prázdná cesta nespadne", taxonomy.normalize_path("") is None)
    check("None nespadne", taxonomy.normalize_path(None) is None)
    check("úplně neznámý top → na model",
          taxonomy.normalize_path("vesmír > planety") is None)

    # Nejnebezpečnější případ: podúroveň sedí, ale pod JINÝM topem. Tiše
    # přesunout „ostatní > sýr“ do mléčných výrobků by přepsalo zařazení,
    # které nikdo nekontroloval.
    check("podúroveň pod cizím topem se nepřebírá",
          taxonomy.normalize_path("ostatní > sýr") is None,
          str(taxonomy.normalize_path("ostatní > sýr")))

    check("stem 'koření'/'kořeny' si nepřebíjí význam",
          taxonomy.normalize_path("koření a bylinky > koření")
          == "koření a bylinky > mleté koření",
          str(taxonomy.normalize_path("koření a bylinky > koření")))


# ─── Převod uložených dat ────────────────────────────────────────────────────

def renormalize_checks():
    print("\npřevod uložených kategorií:")
    db = SessionLocal()
    ok = Ingredient(name_cs="mouka hladká", category_path="obiloviny a pečivo > mouka")
    fix = Ingredient(name_cs="vepřová panenka", category_path="maso > prasine")
    drop = Ingredient(name_cs="něco divného", category_path="sladidla > dezerty")
    db.add_all([ok, fix, drop])
    db.commit()
    ids = (ok.id, fix.id, drop.id)
    db.close()

    res = categorize.renormalize_all()
    check("projde všechny uložené kategorie", res["total"] == 3, str(res))
    check("kanonickou nechá být", res["kept"] == 1, str(res))
    check("pokaženou přepíše", res["changed"] == 1, str(res))
    check("nerozhodnutelnou vymaže", res["cleared"] == 1, str(res))

    db = SessionLocal()
    check("z „prasine“ je „vepřové“",
          db.get(Ingredient, ids[1]).category_path == "maso > vepřové",
          str(db.get(Ingredient, ids[1]).category_path))
    check("top se dorovná podle nové cesty",
          db.get(Ingredient, ids[1]).category == "maso",
          str(db.get(Ingredient, ids[1]).category))
    check("nerozhodnutelná čeká na model (prázdná cesta)",
          db.get(Ingredient, ids[2]).category_path is None)
    db.close()

    again = categorize.renormalize_all()
    check("druhý běh už nic nemění", again["changed"] == 0 and again["cleared"] == 0,
          str(again))


# ─── Dietní tagy ─────────────────────────────────────────────────────────────

def diet_checks():
    print("\ndietní tagy – rozpor se surovinami:")
    # Přesně nahlášený případ.
    got = diet.conflicts("Kořeněné mleté maso s kuskusem",
                         [FakeIng("kuskus"), FakeIng("mleté maso")])
    check("„Kořeněné mleté maso“ není vegetariánské", diet.VEGETARIAN in got, str(got))
    check("a není ani veganské", diet.VEGAN in got, str(got))

    check("maso se pozná z NÁZVU, i když suroviny nic neříkají",
          diet.VEGETARIAN in diet.conflicts("Mleté maso s rýží", [FakeIng("rýže")]))

    check("napárovaná surovina rozhoduje podle kategorie",
          diet.VEGETARIAN in diet.conflicts(
              "Nedělní oběd", [FakeIng("cosi", category_path="maso > hovězí")]))
    check("ryby taky vylučují vegetariánské",
          diet.VEGETARIAN in diet.conflicts(
              "Salát", [FakeIng("tuňák v konzervě")]))

    print("\nveganské vs vegetariánské:")
    got = diet.conflicts("Míchaná vejce", [FakeIng("vejce"), FakeIng("máslo")])
    check("vejce a máslo vylučují veganské", diet.VEGAN in got, str(got))
    check("ale vegetariánské ne", diet.VEGETARIAN not in got, str(got))

    got = diet.conflicts("Kokosová polévka",
                         [FakeIng("kokosové mléko"), FakeIng("mrkev")])
    check("kokosové mléko veganské nevylučuje", got == set(), str(got))
    got = diet.conflicts("Tofu na pánvi", [FakeIng("sójové mléko"), FakeIng("tofu")])
    check("sójové mléko taky ne", got == set(), str(got))
    got = diet.conflicts(
        "Rostlinná verze",
        [FakeIng("cosi", category_path="mléčné výrobky > rostlinné alternativy")])
    check("rostlinná alternativa z číselníku veganské nevylučuje",
          got == set(), str(got))

    print("\nkontrola nesmí být přehnaně horlivá:")
    check("zeleninový recept projde",
          diet.conflicts("Zeleninový salát",
                         [FakeIng("okurka"), FakeIng("rajčata"),
                          FakeIng("olivový olej")]) == set())
    check("ne-surovina se nepočítá",
          diet.conflicts("Zeleninový salát",
                         [FakeIng("alobal", nonfood=True)]) == set())
    check("recept bez surovin nic nevyvrací",
          diet.conflicts("Něco dobrého", []) == set())

    print("\nfiltr navržených tagů:")
    keys = ["chod:hlavni-jidlo", "dieta:vegetarianske", "chut:korenene"]
    kept = diet.allowed_tag_keys("Mleté maso s kuskusem", [FakeIng("mleté maso")], keys)
    check("rozporný dietní tag se zahodí", "dieta:vegetarianske" not in kept, str(kept))
    check("ostatní tagy zůstanou",
          {"chod:hlavni-jidlo", "chut:korenene"} <= kept, str(kept))
    check("u zeleninového receptu se nezahodí nic",
          diet.allowed_tag_keys("Zeleninový salát", [FakeIng("okurka")], keys)
          == set(keys))


def cleanup_checks():
    print("\núklid už uložených tagů:")
    from app.models import RecipeTag, Tag
    from app.modules import tagging

    db = SessionLocal()
    veg = Tag(namespace="dieta", slug=diet.VEGETARIAN, label_cs="Vegetariánské")
    chod = Tag(namespace="chod", slug="hlavni-jidlo", label_cs="Hlavní jídlo")
    db.add_all([veg, chod])
    maso = Ingredient(name_cs="mleté maso", category_path="maso > hovězí")
    db.add(maso)
    db.flush()

    bad = Recipe(title="Kořeněné mleté maso s kuskusem",
                 source_url="https://web.cz/maso")
    good = Recipe(title="Zeleninové rizoto", source_url="https://web.cz/rizoto")
    db.add_all([bad, good])
    db.flush()
    db.add(RecipeIngredient(recipe_id=bad.id, raw_text="500 g mletého masa",
                            ingredient_id=maso.id))
    db.add(RecipeIngredient(recipe_id=good.id, raw_text="rýže"))
    for r in (bad, good):
        db.add(RecipeTag(recipe_id=r.id, tag_id=veg.id))
        db.add(RecipeTag(recipe_id=r.id, tag_id=chod.id))
    db.commit()
    bad_id, good_id, veg_id, chod_id = bad.id, good.id, veg.id, chod.id
    db.close()

    dry = tagging.strip_wrong_diet_tags(dry_run=True)
    check("nanečisto najde jeden recept", dry["recipes"] == 1, str(dry))
    db = SessionLocal()
    still = db.query(RecipeTag).filter_by(recipe_id=bad_id, tag_id=veg_id).count()
    db.close()
    check("nanečisto opravdu nic nemaže", still == 1, str(still))

    res = tagging.strip_wrong_diet_tags()
    check("naostro tag odebere", res["removed"] == 1, str(res))
    db = SessionLocal()
    check("masový recept už vegetariánský není",
          db.query(RecipeTag).filter_by(recipe_id=bad_id, tag_id=veg_id).count() == 0)
    check("ostatní tagy zůstaly netknuté",
          db.query(RecipeTag).filter_by(recipe_id=bad_id, tag_id=chod_id).count() == 1)
    check("zeleninovému receptu se nesáhlo",
          db.query(RecipeTag).filter_by(recipe_id=good_id, tag_id=veg_id).count() == 1)
    db.close()

    again = tagging.strip_wrong_diet_tags()
    check("druhý běh už nic neodebírá", again["removed"] == 0, str(again))


def main():
    taxonomy_checks()
    renormalize_checks()
    diet_checks()
    cleanup_checks()
    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
