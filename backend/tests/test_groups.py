"""Testy seskupení receptů do kategorií podle názvu.

V korpusu je 12 398 názvů se dvěma a víc recepty (největší shluk 15×
„těstovinový salát"). Ve výpisu je chceme vidět jako JEDNU kategorii
s počtem variant a po rozkliknutí seznam variant.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-groups-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Recipe  # noqa: E402
from app.modules.textnorm import refresh_search_text, title_key  # noqa: E402

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


def _add(db, title, *, rating=None, domain="a.cz", n=[0]):
    n[0] += 1
    r = Recipe(title=title, source_url=f"http://t/{n[0]}", source_domain=domain,
               rating=rating, instructions="Vše smícháme a podáváme.")
    db.add(r)
    db.flush()
    refresh_search_text(r)
    return r


def main():
    # ── klíč sám o sobě ──
    check("pořadí slov nehraje roli",
          title_key("těstovinový salát") == title_key("Salát těstovinový"),
          f"{title_key('těstovinový salát')} vs {title_key('Salát těstovinový')}")
    check("skloňování nehraje roli",
          title_key("těstovinový salát") == title_key("Těstovinové saláty"))
    check("slovo navíc dělá jinou kategorii",
          title_key("těstovinový salát") != title_key("těstovinový salát s kuřecím masem"))
    check("jiné jídlo má jiný klíč",
          title_key("bramborový salát") != title_key("těstovinový salát"))
    check("prázdný název dá prázdný klíč", title_key("") == "" and title_key(None) == "")

    with TestClient(app) as c:
        db = SessionLocal()
        try:
            varianty = [
                _add(db, "Těstovinový salát", rating=3.0),
                _add(db, "Salát těstovinový", rating=4.5, domain="b.cz"),
                _add(db, "Těstovinové saláty", rating=4.0, domain="c.cz"),
            ]
            jiny = _add(db, "Bramborový salát", rating=5.0)
            delsi = _add(db, "Těstovinový salát s kuřecím masem", rating=2.0)
            db.commit()
            key = varianty[0].title_key
            ids = {r.id for r in varianty}
            db.commit()

            check("recept dostal title_key při uložení", bool(key), repr(key))

            # ── negroupovaný výpis vrací všechno ──
            r = c.get("/api/recipes", params={"limit": 100})
            check("bez seskupení jsou vidět všechny recepty",
                  r.json()["total"] == 5, str(r.json()["total"]))
            check("bez seskupení je variants=1",
                  all(it["variants"] == 1 for it in r.json()["items"]))
            check("bez seskupení není group_key",
                  all(it["group_key"] is None for it in r.json()["items"]))

            # ── seskupený výpis ──
            r = c.get("/api/recipes", params={"group": True, "limit": 100})
            body = r.json()
            check("seskupením se počet položek sníží", body["total"] == 3,
                  str(body["total"]))
            grp = [it for it in body["items"] if it["group_key"] == key]
            check("kategorie je ve výpisu jednou", len(grp) == 1, str(len(grp)))
            check("kategorie hlásí 3 varianty",
                  grp and grp[0]["variants"] == 3, str(grp[0]["variants"]) if grp else "")
            check("reprezentant je jedna z variant",
                  grp and grp[0]["id"] in ids, str(grp[0]["id"]) if grp else "")
            check("delší název zůstal samostatně",
                  any(it["id"] == delsi.id and it["variants"] == 1 for it in body["items"]))
            check("jiné jídlo zůstalo samostatně",
                  any(it["id"] == jiny.id and it["variants"] == 1 for it in body["items"]))

            # ── varianty jedné kategorie ──
            r = c.get(f"/api/recipes/groups/{key}")
            body = r.json()
            check("endpoint variant vrátí všechny tři",
                  r.status_code == 200 and body["total"] == 3, str(body)[:150])
            check("vrací se právě varianty té kategorie",
                  {it["id"] for it in body["items"]} == ids,
                  str({it["id"] for it in body["items"]}))
            check("varianty jsou seřazené podle hodnocení",
                  [it["rating"] for it in body["items"]] == [4.5, 4.0, 3.0],
                  str([it["rating"] for it in body["items"]]))
            check("varianty nesou group_key",
                  all(it["group_key"] == key for it in body["items"]))
            check("cesta 'groups' se nesplete s detailem receptu",
                  c.get("/api/recipes/groups/neexistuje").status_code == 200)

            # ── seskupení respektuje filtry a řazení ──
            r = c.get("/api/recipes", params={"group": True, "q": "bramborový"})
            check("filtr hledání se na seskupení uplatní",
                  r.json()["total"] == 1 and r.json()["items"][0]["id"] == jiny.id,
                  str(r.json())[:150])
            r = c.get("/api/recipes", params={"group": True, "sort": "rating",
                                              "limit": 100})
            check("řazení podle hodnocení bere nejlepší z kategorie",
                  r.json()["items"][0]["id"] == jiny.id,
                  str(r.json()["items"][0]["id"]))

            # ── recept bez klíče se nesmí slít s ostatními ──
            bezklice = _add(db, "?!", domain="d.cz")
            db.commit()
            check("název bez slov nemá klíč", not bezklice.title_key, repr(bezklice.title_key))
            r = c.get("/api/recipes", params={"group": True, "limit": 100})
            body = r.json()
            check("recept bez klíče se nesdružuje ani nezmizí",
                  body["total"] == 4, str(body["total"]))
            solo = [it for it in body["items"] if it["id"] == bezklice.id]
            check("bez klíče je to samostatná položka bez kategorie",
                  solo and solo[0]["group_key"] is None and solo[0]["variants"] == 1,
                  str(solo))
        finally:
            db.close()

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
