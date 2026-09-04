"""Testy pořadí na úvodní stránce a skrývání receptů.

Regresní scénář z produkce (viz print úvodní stránky): první stránka byla
plná návodů na zdobení dortů a míchaných nápojů. Příčina nebyla v datech,
ale v řazení — recept BEZ jediné napárované suroviny má `ing_total = 0`,
takže mu „chybí" nula surovin, a smart řazení (`ORDER BY missing`) ho
vyneslo nad všechno ostatní i s cedulkou „Můžeš vařit".
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-feed-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Ingredient, PantryItem, Recipe, RecipeIngredient  # noqa: E402
from app.modules import feed  # noqa: E402

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


def main():
    now = datetime.now(timezone.utc)

    # ── skóre: čistá funkce ────────────────────────────────────────────
    dekorace = feed.score(rating=5.0, rating_count=22, ing_total=0,
                          instr_chars=60, created_at=now, now=now)
    recept = feed.score(rating=4.4, rating_count=500, ing_total=9,
                        instr_chars=600, created_at=now, now=now)
    check("dekorace bez surovin je hluboko pod pořádným receptem",
          dekorace < recept - 2, f"{dekorace} vs {recept}")

    malo_hlasu = feed.score(rating=5.0, rating_count=1, ing_total=8,
                            instr_chars=500, created_at=now, now=now)
    hodne_hlasu = feed.score(rating=4.4, rating_count=500, ing_total=8,
                             instr_chars=500, created_at=now, now=now)
    check("5,0 od jednoho nepřebije 4,4 od pěti set",
          malo_hlasu < hodne_hlasu, f"{malo_hlasu} vs {hodne_hlasu}")

    stary = feed.score(rating=4.4, rating_count=50, ing_total=8, instr_chars=500,
                       created_at=now - timedelta(days=400), now=now)
    novy = feed.score(rating=4.4, rating_count=50, ing_total=8, instr_chars=500,
                      created_at=now, now=now)
    check("čerstvý recept má malou přirážku", novy > stary, f"{novy} vs {stary}")
    check("čerstvost nepřebije kvalitu", novy - stary < 1.0, f"{novy - stary}")

    check("bez hodnocení skóre nespadne na nulu",
          feed.score(rating=None, rating_count=None, ing_total=8,
                     instr_chars=500) > 3.0)
    check("prázdný postup srazí skóre",
          feed.score(rating=4.5, rating_count=50, ing_total=8, instr_chars=0)
          < feed.score(rating=4.5, rating_count=50, ing_total=8, instr_chars=500))

    with TestClient(app) as c:
        db = SessionLocal()
        try:
            mouka = Ingredient(name_cs="mouka", kcal_100g=364)
            db.add(mouka)
            db.flush()

            # Přesně situace z produkce: dekorace má lepší hodnocení, ale
            # ani jednu napárovanou surovinu.
            dek = Recipe(title="Květinový dort - zdobení", source_url="http://t/d",
                         rating=5.0, rating_count=22, ing_total=0,
                         instructions="Zdobení\nVše je ruční práce.")
            dobry = Recipe(title="Bramborová polévka", source_url="http://t/p",
                           rating=4.4, rating_count=500, ing_total=1,
                           instructions="Brambory oloupeme, nakrájíme na kostky "
                                        "a vaříme v osolené vodě asi 20 minut, "
                                        "než změknou. Zahustíme jíškou z mouky.")
            db.add_all([dek, dobry])
            db.flush()
            db.add(RecipeIngredient(recipe_id=dobry.id, raw_text="100 g mouka",
                                    ingredient_id=mouka.id))
            db.commit()
            dek_id, dobry_id = dek.id, dobry.id

            feed.recompute_all()

            # ── doporučené pořadí ──
            r = c.get("/api/recipes", params={"limit": 50})
            items = r.json()["items"]
            order = [it["id"] for it in items]
            check("výchozí řazení je doporučené (dekorace není první)",
                  order and order[0] == dobry_id, str(order))
            check("dekorace je až za pořádným receptem",
                  order.index(dek_id) > order.index(dobry_id), str(order))

            # ── „Můžeš vařit" nesmí svítit bez napárovaných surovin ──
            dek_card = next(it for it in items if it["id"] == dek_id)
            check("recept bez surovin hlásí total = 0", dek_card["total"] == 0,
                  str(dek_card["total"]))
            check("a missing = 0 (proto se dřív tvářil jako hotový)",
                  dek_card["missing_count"] == 0)

            # ── filtry dostupnosti ho nesmí pouštět ──
            db.add(PantryItem(ingredient_id=mouka.id))
            db.commit()
            r = c.get("/api/recipes", params={"only_have": True, "limit": 50})
            ids = [it["id"] for it in r.json()["items"]]
            check("'můžu uvařit teď' nepustí recept bez surovin",
                  dek_id not in ids, str(ids))
            check("ale pustí recept, na který suroviny mám", dobry_id in ids, str(ids))

            r = c.get("/api/recipes", params={"max_missing": 5, "limit": 50})
            check("'max chybí' taky nepustí recept bez surovin",
                  dek_id not in [it["id"] for it in r.json()["items"]])

            # ── smart řazení: bez surovin až za ostatní ──
            r = c.get("/api/recipes", params={"sort": "smart", "limit": 50})
            order = [it["id"] for it in r.json()["items"]]
            check("smart řazení strká recepty bez surovin dozadu",
                  order.index(dek_id) > order.index(dobry_id), str(order))

            # ── skrývání ──
            resp = c.patch(f"/api/recipes/{dek_id}/hidden", json={"hidden": True})
            check("skrytí projde a vrátí detail",
                  resp.status_code == 200 and resp.json()["hidden"] is True,
                  str(resp.json())[:120])
            r = c.get("/api/recipes", params={"limit": 50})
            check("skrytý recept ve výpisu není",
                  dek_id not in [it["id"] for it in r.json()["items"]])
            check("a nezapočítá se ani do celkového počtu",
                  r.json()["total"] == 1, str(r.json()["total"]))
            r = c.get("/api/recipes", params={"limit": 50, "show_hidden": True})
            check("se show_hidden je zase vidět",
                  dek_id in [it["id"] for it in r.json()["items"]])
            check("skrytý recept jde pořád otevřít",
                  c.get(f"/api/recipes/{dek_id}").status_code == 200)

            resp = c.patch(f"/api/recipes/{dek_id}/hidden", json={"hidden": False})
            check("vrácení zpátky projde", resp.json()["hidden"] is False)
            r = c.get("/api/recipes", params={"limit": 50})
            check("a recept je zase ve výpisu",
                  dek_id in [it["id"] for it in r.json()["items"]])

            check("skrytí neexistujícího receptu je 404",
                  c.patch("/api/recipes/99999/hidden", json={"hidden": True})
                  .status_code == 404)

            # ── vypnutá spíž ───────────────────────────────────────────
            # Kdo si spíž neplní, nemá u každého receptu koukat na
            # „chybí 9 surovin". Vypnutá spíž se chová jako prázdná, ale
            # filtry a řazení podle ní se ignorují místo prázdného výsledku.
            from app.config import settings  # noqa: PLC0415
            from app.modules.pantry import pantry_ingredient_ids  # noqa: PLC0415

            settings.set_admin("pantry_enabled", False)
            try:
                check("vypnutá spíž se tváří jako prázdná",
                      pantry_ingredient_ids(db) == set())
                check("health hlásí vypnutou spíž",
                      c.get("/api/health").json()["pantry"] is False)

                r = c.get("/api/recipes", params={"only_have": True, "limit": 50})
                ids = [it["id"] for it in r.json()["items"]]
                check("'můžu uvařit teď' se ignoruje, ne že nic nevrátí",
                      dobry_id in ids and dek_id in ids, str(ids))
                r = c.get("/api/recipes", params={"max_missing": 0, "limit": 50})
                check("'max chybí' se taky ignoruje",
                      len(r.json()["items"]) == 2, str(r.json()["total"]))
                r = c.get("/api/recipes", params={"sort": "smart", "limit": 50})
                order = [it["id"] for it in r.json()["items"]]
                check("smart spadne na doporučené pořadí",
                      order[0] == dobry_id, str(order))
            finally:
                settings.set_admin("pantry_enabled", True)
            check("zapnutá spíž zase počítá",
                  c.get("/api/health").json()["pantry"] is True)
            r = c.get("/api/recipes", params={"only_have": True, "limit": 50})
            check("a filtr zase filtruje",
                  dek_id not in [it["id"] for it in r.json()["items"]])

            # ── skóre je opravdu v DB ──
            # Přepočet běžel ve vlastní session; tahle má expire_on_commit=False,
            # takže by jinak vracela hodnoty z doby před přepočtem.
            db.expire_all()
            fresh = db.get(Recipe, dobry_id)
            check("recept má spočítané skóre", fresh.feed_score is not None,
                  str(fresh.feed_score))
            check("dekorace má nižší skóre",
                  db.get(Recipe, dek_id).feed_score < fresh.feed_score)

            # ── délku postupu počítá databáze, a to ve ZNACÍCH ──
            # recompute_all kvůli `len()` netahá text postupu do paměti
            # (171k receptů = stovky MB), počítá ho SQL funkcí. Na MariaDB by
            # LENGTH vracelo bajty a česká diakritika by postup nafoukla nad
            # práh – proto CHAR_LENGTH. Tenhle postup má 100 znaků, ale ve
            # UTF-8 přes 120 bajtů: musí dostat postih za krátký postup.
            diakritika = "Žluťoučký kůň úpěl ďábelské ódy, přičemž míchal řídké těsto. " * 3
            diakritika = diakritika[:100]  # 100 znaků / 134 bajtů
            kratky = Recipe(title="Krátký postup s diakritikou", source_url="u:kratky",
                            rating=4.5, rating_count=50, ing_total=8,
                            instructions=diakritika)
            db.add(kratky)
            db.commit()
            kratky_id = kratky.id
            feed.recompute_all()
            db.expire_all()

            check("postup se měří na 100 znaků, ne na 120+ bajtů",
                  len(diakritika) == 100 and len(diakritika.encode()) > 120,
                  f"{len(diakritika)} znaků / {len(diakritika.encode())} bajtů")
            expected = feed.score(rating=4.5, rating_count=50, ing_total=8,
                                  instr_chars=100, created_at=kratky.created_at,
                                  now=datetime.now(timezone.utc))
            check("krátký postup dostane postih i s diakritikou",
                  abs(db.get(Recipe, kratky_id).feed_score - expected) < 0.05,
                  f"{db.get(Recipe, kratky_id).feed_score} vs {expected}")
        finally:
            db.close()

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
