"""Testy rodičovských vazeb surovin a filtrů ve „Vařím z".

Tři nahlášené věci:

  1. Kdo vybral ve „Vařím z" surovinu „rýže", nenašel recept s „arborio rýží" –
     jsou to dva různé záznamy slovníku bez jakéhokoli vztahu.
  2. V režimu „Vařím z" se schovávaly VŠECHNY filtry, takže nešlo chtít
     „z rýže, vegetariánské a indické".
  3. (frontend) Filtry se ztrácely při návratu z detailu receptu – řeší se
     přesunem do URL, backend s tím nemá co dělat.

Vazby se odvozují z názvů, bez modelu: rodičem je surovina, jejíž kmeny jsou
vlastní podmnožinou. Testy hlídají hlavně směr té vazby (obecné → konkrétní,
ne naopak) a to, že se nevyrábí nesmyslná spojení přes obecná přídavná jména.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-tree-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Ingredient, Recipe, RecipeIngredient, RecipeTag, Tag,
)
from app.modules import ingredient_tree  # noqa: E402

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


NAMES = [
    "rýže", "arborio rýže", "jasmínová rýže", "rýže basmati",
    "olej", "olivový olej", "slunečnicový olej",
    "mouka", "hladká mouka", "polohrubá mouka",
    "kuřecí prsa", "kuřecí prsa bez kosti",
    "sůl", "cukr", "čerstvá bazalka",
]

INSTR = ("Rýži propláchneme a uvaříme v osolené vodě. Na oleji osmahneme "
         "cibuli, přidáme rýži a zalijeme vývarem. Vaříme 20 minut.")


def seed_db():
    db = SessionLocal()
    ings = {}
    for name in NAMES:
        i = Ingredient(name_cs=name)
        db.add(i)
        db.flush()
        ings[name] = i.id

    veg = Tag(namespace="dieta", slug="vegetarianske", label_cs="Vegetariánské")
    ind = Tag(namespace="kuchyne", slug="indicka", label_cs="Indická")
    db.add_all([veg, ind])
    db.flush()

    def recipe(title, ing_names, tags=()):
        r = Recipe(title=title, source_url=f"https://web.cz/{title}",
                   source_domain="web.cz", instructions=INSTR)
        db.add(r)
        db.flush()
        for n in ing_names:
            db.add(RecipeIngredient(recipe_id=r.id, raw_text=n,
                                    ingredient_id=ings[n]))
        for t in tags:
            db.add(RecipeTag(recipe_id=r.id, tag_id=t.id))
        return r.id

    rid = {
        "risotto": recipe("Rizoto", ["arborio rýže", "olivový olej"]),
        "kari": recipe("Zeleninové kari", ["jasmínová rýže", "olej"], [veg, ind]),
        "prosta": recipe("Rýže na másle", ["rýže", "sůl"]),
        "kure": recipe("Kuřecí prsa", ["kuřecí prsa bez kosti", "sůl"]),
    }
    db.commit()
    db.close()
    return ings, rid


def main():
    ings, rid = seed_db()

    print("\nstavba stromu:")
    res = ingredient_tree.build()
    check("projde všechny suroviny", res["total"] == len(NAMES), str(res))
    check("něco se propojí", res["linked"] > 0, str(res))

    db = SessionLocal()
    parent = {i.name_cs: (db.get(Ingredient, i.parent_id).name_cs
                          if i.parent_id else None)
              for i in db.scalars(__import__("sqlalchemy").select(Ingredient))}
    db.close()

    for child, want in [
        ("arborio rýže", "rýže"),
        ("jasmínová rýže", "rýže"),
        ("rýže basmati", "rýže"),
        ("olivový olej", "olej"),
        ("slunečnicový olej", "olej"),
        ("hladká mouka", "mouka"),
    ]:
        check(f"„{child}“ patří pod „{want}“", parent.get(child) == want,
              str(parent.get(child)))

    check("obecná surovina rodiče nemá", parent.get("rýže") is None,
          str(parent.get("rýže")))
    check("nesouvisející surovina rodiče nemá", parent.get("sůl") is None,
          str(parent.get("sůl")))
    check("nejkonkrétnější rodič vyhrává nad obecnějším",
          parent.get("kuřecí prsa bez kosti") == "kuřecí prsa",
          str(parent.get("kuřecí prsa bez kosti")))
    # „čerstvá bazalka" nesmí skončit pod ničím jen proto, že sdílí obecné
    # přídavné jméno – proto stoplist kmenů typu „čerstvý", „mletý".
    check("obecné přídavné jméno rodiče nedělá",
          parent.get("čerstvá bazalka") is None, str(parent.get("čerstvá bazalka")))

    again = ingredient_tree.build()
    check("druhý běh nic nemění", again["changed"] == 0, str(again))
    dry = ingredient_tree.build(dry_run=True)
    check("běh nanečisto nic nezapisuje", dry["dry_run"] is True and dry["changed"] == 0)

    print("\nrozbalení výběru:")
    db = SessionLocal()
    exp = ingredient_tree.expand(db, [ings["rýže"]])
    check("„rýže“ zastupuje i své varianty",
          {ings["arborio rýže"], ings["jasmínová rýže"], ings["rýže basmati"]} <= exp,
          str(sorted(exp)))
    check("obsahuje i sebe", ings["rýže"] in exp)
    exp2 = ingredient_tree.expand(db, [ings["arborio rýže"]])
    check("konkrétní surovina obecnou NEzastupuje",
          exp2 == {ings["arborio rýže"]}, str(sorted(exp2)))
    check("prázdný výběr zůstane prázdný",
          ingredient_tree.expand(db, []) == set())
    db.close()

    with TestClient(app) as c:
        print("\n„Vařím z“ – rýže najde i arborio:")
        r = c.get("/api/recipes/cook-from",
                  params={"ingredient_ids": [ings["rýže"]]}).json()
        titles = {x["title"] for x in r}
        check("rizoto s arborio rýží se najde", "Rizoto" in titles, str(titles))
        check("kari s jasmínovou rýží taky", "Zeleninové kari" in titles, str(titles))
        check("recept s obyčejnou rýží samozřejmě taky",
              "Rýže na másle" in titles, str(titles))
        check("recept bez rýže se neplete dovnitř",
              "Kuřecí prsa" not in titles, str(titles))

        r = c.get("/api/recipes/cook-from",
                  params={"ingredient_ids": [ings["arborio rýže"]]}).json()
        titles = {x["title"] for x in r}
        check("opačný směr neplatí – arborio nenajde obyčejnou rýži",
              titles == {"Rizoto"}, str(titles))

        print("\n„Vařím z“ + filtry:")
        r = c.get("/api/recipes/cook-from", params={
            "ingredient_ids": [ings["rýže"]], "tags": ["dieta:vegetarianske"],
        }).json()
        titles = {x["title"] for x in r}
        check("filtr na tag zabere", titles == {"Zeleninové kari"}, str(titles))

        r = c.get("/api/recipes/cook-from", params={
            "ingredient_ids": [ings["rýže"]],
            "tags": ["dieta:vegetarianske", "kuchyne:indicka"],
        }).json()
        check("víc jmenných prostorů = ZÁROVEŇ",
              {x["title"] for x in r} == {"Zeleninové kari"}, str(r))

        r = c.get("/api/recipes/cook-from", params={
            "ingredient_ids": [ings["rýže"]], "tags": ["kuchyne:indicka"],
            "q": "kari",
        }).json()
        check("text a tag jdou dohromady",
              {x["title"] for x in r} == {"Zeleninové kari"}, str(r))

        r = c.get("/api/recipes/cook-from", params={
            "ingredient_ids": [ings["rýže"]], "q": "rizoto",
        }).json()
        check("samotný text taky funguje",
              {x["title"] for x in r} == {"Rizoto"}, str(r))

        r = c.get("/api/recipes/cook-from", params={
            "ingredient_ids": [ings["rýže"]], "tags": ["dieta:neexistuje"],
        }).json()
        check("neexistující tag nic nevrátí, ale nespadne", r == [], str(r))

        print("\nskryté recepty:")
        c.patch(f"/api/recipes/{rid['risotto']}/hidden", json={"hidden": True})
        r = c.get("/api/recipes/cook-from",
                  params={"ingredient_ids": [ings["rýže"]]}).json()
        check("ručně skrytý recept se ve „Vařím z“ neukáže",
              "Rizoto" not in {x["title"] for x in r}, str(r))

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
