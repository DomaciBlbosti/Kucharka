"""Testy ruční kontroly receptů (záložka Kontrola).

Kontrola je nástroj na projití korpusu očima: člověk vidí, co přišlo ze
zdroje, vedle toho, co appka ukazuje, a rozhodne se. Testy hlídají tři věci,
na kterých to stojí:

  * stránkování musí být STABILNÍ – uložení štítku nesmí přeházet pořadí,
    jinak by člověk recepty přeskakoval nebo viděl stejný dvakrát,
  * štítek „není recept" musí recept opravdu skrýt z výpisů (a odebrání ho
    zase odkrýt) – jinak by se dalo hodiny klikat a nic by se nezměnilo,
  * kontrola nesmí přepsat `hidden` u receptu, který jí nikdy neprošel.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-review-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Ingredient, Recipe, RecipeIngredient, RecipeReview,
)

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


LONG = ("Maso nakrájíme na kostky a opečeme dozlatova. Přidáme cibuli a "
        "mrkev, zalijeme vývarem a dusíme 40 minut pod pokličkou. Nakonec "
        "zahustíme moukou a necháme přejít varem.")


def seed_db():
    db = SessionLocal()
    ing = Ingredient(name_cs="mouka hladká", kcal_100g=350)
    db.add(ing)
    db.flush()
    ids = []
    for i in range(25):
        r = Recipe(
            title=f"Recept {i:02d}", source_url=f"https://web.cz/{i}",
            source_domain="web.cz" if i % 2 else "jiny.cz",
            instructions=LONG if i % 5 else "Smíchat.",
            original_instructions="Mix it." if i % 7 == 0 else None,
            original_title="Recipe" if i % 7 == 0 else None,
        )
        db.add(r)
        db.flush()
        ids.append(r.id)
        # každý třetí má nenapárovanou surovinu
        db.add(RecipeIngredient(
            recipe_id=r.id, raw_text="200 g mouky",
            ingredient_id=None if i % 3 == 0 else ing.id,
        ))
    db.commit()
    db.close()
    return ids


def main():
    ids = seed_db()

    with TestClient(app) as c:
        # ── nabídka ──
        print("\nnabídka:")
        r = c.get("/api/review/labels").json()
        slugs = [d["slug"] for d in r["labels"]]
        check("štítky dorazí ze serveru", "zkontrolovano" in slugs and
              "neni-recept" in slugs, str(slugs))
        check("výběrové režimy taky", "unmatched" in r["picks"], str(r["picks"]))
        check("u štítku je vidět, že skrývá",
              next(d for d in r["labels"] if d["slug"] == "neni-recept")["hides"]
              is True)

        # ── stránkování ──
        print("\nstránkování:")
        p1 = c.get("/api/review/recipes", params={"per_page": 10, "page": 1}).json()
        check("stránka má tolik receptů, kolik má mít", len(p1["items"]) == 10,
              str(len(p1["items"])))
        check("celkový počet sedí", p1["total"] == 25, str(p1["total"]))
        check("počet stran sedí", p1["pages"] == 3, str(p1["pages"]))
        p2 = c.get("/api/review/recipes", params={"per_page": 10, "page": 2}).json()
        check("druhá stránka je jiná",
              {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]} == set())
        p3 = c.get("/api/review/recipes", params={"per_page": 10, "page": 3}).json()
        check("poslední stránka má zbytek", len(p3["items"]) == 5,
              str(len(p3["items"])))
        far = c.get("/api/review/recipes", params={"per_page": 10, "page": 99}).json()
        check("stránka za koncem nespadne, vrátí poslední", far["page"] == 3,
              str(far["page"]))

        # ── co stránka nese ──
        print("\nobsah stránky:")
        item = p1["items"][0]
        for key in ("title", "instructions", "original_instructions",
                    "ingredients", "metrics", "tags", "review", "n_unmatched"):
            check(f"payload obsahuje {key}", key in item, str(sorted(item)))
        check("suroviny nesou výsledek párování",
              "unmatched" in item["ingredients"][0])

        # Suroviny musí být v pořadí z receptu, ne jak je vrátí databáze –
        # čtou se vedle postupu, přeházené jsou k nepoužití.
        db = SessionLocal()
        multi = Recipe(title="Víc surovin", source_url="https://web.cz/multi",
                       source_domain="web.cz", instructions=LONG)
        db.add(multi)
        db.flush()
        order = ["nejdřív mouka", "potom mléko", "nakonec vejce"]
        for txt in order:
            db.add(RecipeIngredient(recipe_id=multi.id, raw_text=txt))
        db.commit()
        multi_id = multi.id
        db.close()
        got = c.get("/api/review/recipes", params={"per_page": 50}).json()
        rows = next(i for i in got["items"] if i["id"] == multi_id)["ingredients"]
        check("suroviny drží pořadí z receptu",
              [x["raw_text"] for x in rows] == order,
              str([x["raw_text"] for x in rows]))
        check("metriky nesou pokrytí surovin", "ingr_coverage" in item["metrics"])
        check("syrová data se defaultně netahají (jsou velká)",
              item["raw_json"] == "", item["raw_json"][:40])
        withraw = c.get("/api/review/recipes",
                        params={"per_page": 1, "include_raw": True}).json()
        check("ale na požádání ano", "raw_json" in withraw["items"][0])

        # ── uložení štítku ──
        print("\nuložení rozhodnutí:")
        rid = ids[1]
        r = c.put(f"/api/review/{rid}", json={"labels": ["zkontrolovano"]})
        check("uloží se", r.status_code == 200, str(r.status_code))
        check("štítek se vrátí", r.json()["labels"] == ["zkontrolovano"])
        check("zkontrolovaný recept se neskrývá", r.json()["hidden"] is False)

        got = c.get("/api/review/recipes", params={"per_page": 50}).json()
        mine = next(i for i in got["items"] if i["id"] == rid)
        check("štítek se načte zpátky",
              mine["review"]["labels"] == ["zkontrolovano"], str(mine["review"]))
        check("je vidět, kdy se kontrolovalo",
              mine["review"]["reviewed_at"] is not None)

        r = c.put(f"/api/review/{rid}", json={"labels": ["zkontrolovano",
                                                         "spatny-preklad"],
                                              "note": "půlka věty chybí"})
        check("štítků může být víc najednou",
              r.json()["labels"] == ["zkontrolovano", "spatny-preklad"],
              str(r.json()["labels"]))
        check("poznámka se uloží", r.json()["note"] == "půlka věty chybí")

        r = c.put(f"/api/review/{rid}", json={"labels": ["vymyslene"]})
        check("neznámý štítek se zahodí", r.json()["labels"] == [],
              str(r.json()["labels"]))

        # ── skrývání ──
        print("\nštítek „není recept“ skryje:")
        bad = ids[2]
        r = c.put(f"/api/review/{bad}", json={"labels": ["neni-recept"]})
        check("štítek „není recept“ recept skryje", r.json()["hidden"] is True)
        db = SessionLocal()
        check("a je to opravdu v databázi", db.get(Recipe, bad).hidden is True)
        db.close()
        r = c.put(f"/api/review/{bad}", json={"labels": ["zkontrolovano"]})
        check("odebrání štítku recept zase odkryje", r.json()["hidden"] is False)

        # Ruční skrytí z detailu receptu nesmí kontrola shodit.
        untouched = ids[3]
        db = SessionLocal()
        db.get(Recipe, untouched).hidden = True
        db.commit()
        db.close()
        got = c.get("/api/review/recipes", params={"per_page": 50}).json()
        check("recept skrytý ručně zůstane skrytý, dokud kontrolou neprojde",
              next(i for i in got["items"] if i["id"] == untouched)["hidden"] is True)

        # ── zrušení kontroly ──
        print("\nzrušení kontroly:")
        r = c.put(f"/api/review/{rid}", json={"labels": [], "note": ""})
        check("prázdné rozhodnutí kontrolu zruší", r.json()["labels"] == [])
        db = SessionLocal()
        left = db.query(RecipeReview).filter_by(recipe_id=rid).count()
        db.close()
        check("a záznam z databáze zmizí", left == 0, str(left))

        # ── filtr nezkontrolovaných ──
        print("\nfiltry:")
        c.put(f"/api/review/{ids[4]}", json={"labels": ["zkontrolovano"]})
        c.put(f"/api/review/{ids[5]}", json={"labels": ["zkontrolovano"]})
        allr = c.get("/api/review/recipes", params={"per_page": 50}).json()
        reviewed = c.get("/api/review/stats").json()["reviewed"]
        un = c.get("/api/review/recipes",
                   params={"per_page": 50, "only_unreviewed": True}).json()
        check("zkontrolované zmizí z fronty",
              un["total"] == allr["total"] - reviewed,
              f'{un["total"]} = {allr["total"]} − {reviewed}?')
        check("a nejsou mezi položkami",
              ids[4] not in {i["id"] for i in un["items"]})

        dom = c.get("/api/review/recipes",
                    params={"per_page": 50, "domain": "jiny.cz"}).json()
        check("filtr na doménu funguje",
              all(i["source_domain"] == "jiny.cz" for i in dom["items"])
              and dom["total"] > 0, str(dom["total"]))

        unm = c.get("/api/review/recipes",
                    params={"per_page": 50, "pick": "unmatched"}).json()
        check("výběr 'unmatched' dá jen recepty s nenapárovanou surovinou",
              all(i["n_unmatched"] > 0 for i in unm["items"]) and unm["total"] > 0,
              str(unm["total"]))

        tr = c.get("/api/review/recipes",
                   params={"per_page": 50, "pick": "translated"}).json()
        check("výběr 'translated' dá jen přeložené",
              all(i["translated"] for i in tr["items"]) and tr["total"] > 0,
              str(tr["total"]))

        bad = c.get("/api/review/recipes", params={"pick": "nesmysl"})
        check("neznámý výběr vrátí 400", bad.status_code == 400,
              str(bad.status_code))

        # ── stabilita stránkování ──
        # Tohle je jádro použitelnosti: kdyby se řadilo podle pokrytí surovin,
        # uložení štítku by pořadí přeházelo a člověk by recepty přeskakoval.
        print("\nstabilita stránkování:")
        before = [i["id"] for i in
                  c.get("/api/review/recipes", params={"per_page": 10}).json()["items"]]
        c.put(f"/api/review/{before[0]}", json={"labels": ["spatne-suroviny"]})
        after = [i["id"] for i in
                 c.get("/api/review/recipes", params={"per_page": 10}).json()["items"]]
        check("uložení štítku nepřeháže stránku", before == after,
              f"{before} vs {after}")

        # ── statistika ──
        print("\nstatistika:")
        st = c.get("/api/review/stats").json()
        db = SessionLocal()
        real_total = db.query(Recipe).count()
        db.close()
        check("statistika zná celkový počet", st["total_recipes"] == real_total,
              f'{st["total_recipes"]} vs {real_total}')
        check("počítá zkontrolované", st["reviewed"] >= 3, str(st["reviewed"]))
        check("zbývá = celkem − zkontrolované",
              st["remaining"] == st["total_recipes"] - st["reviewed"])
        check("rozpad po štítcích", st["by_label"]["zkontrolovano"] >= 2,
              str(st["by_label"]))

        r = c.put("/api/review/999999", json={"labels": ["zkontrolovano"]})
        check("neexistující recept vrátí 404", r.status_code == 404,
              str(r.status_code))

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
