"""Testy cookie přihlašování, hledání, backfillu (fuzzy) a spolehlivosti výživy.

Běží nad SQLite přes TestClient, bez sítě. Spuštění (z backend/):
    python -m tests.test_auth_and_search  (nebo pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Ingredient, IngredientAlias, Recipe, RecipeIngredient  # noqa: E402
from app.modules import backfill  # noqa: E402
from app.modules.lookup import make_lookup_key  # noqa: E402
from app.routers.auth import COOKIE_NAME  # noqa: E402

# Simulace produkční DB: legacy alias "alobal" (jen `alias`, bez lookup_key)
# existuje UŽ PŘED startem appky. Seed builtin ne-surovin na něj nesmí
# spadnout (unique je i sloupec alias) – přesně tohle položilo start
# v produkci ("Application startup failed" + restart smyčka).
Base.metadata.create_all(engine)
_db = SessionLocal()
_db.add(IngredientAlias(alias="alobal", ingredient_id=None))
_db.commit()
_db.close()


def _seed(db) -> dict:
    kure = Ingredient(name_cs="kuřecí prsa", kcal_100g=110, source="nutridb")
    tajna = Ingredient(name_cs="tajná surovina", kcal_100g=100, source="ollama")
    db.add_all([kure, tajna])
    db.flush()
    r = Recipe(title="Segedínský guláš testovací", source_url="http://test/g1", servings=2)
    db.add(r)
    db.flush()
    db.add_all([
        # napárovaný řádek s poctivou výživou
        RecipeIngredient(recipe_id=r.id, raw_text="200 g kuřecí prsa",
                         ingredient_id=kure.id, amount=200, unit="g", grams=200, kcal=220),
        # napárovaný řádek na LLM-odhadnutou surovinu → počítá se jako odhad
        RecipeIngredient(recipe_id=r.id, raw_text="100 g tajná surovina",
                         ingredient_id=tajna.id, amount=100, unit="g", grams=100, kcal=100),
        # nenapárovaný řádek pro backfill (fuzzy na "kuřecí prsa")
        RecipeIngredient(recipe_id=r.id, raw_text="2 ks kuřecích prs"),
        # nadpis skupiny (5 slov) – musí ho smazat automatický purge
        RecipeIngredient(recipe_id=r.id, raw_text="Na vymazání a vysypání formy:"),
        # ozdobný oddělovač – po normalizaci prázdný klíč → purge ho smaže
        RecipeIngredient(recipe_id=r.id, raw_text="-----"),
        # builtin ne-surovina – zůstane nenapárovaná, ale slovník ji zná
        RecipeIngredient(recipe_id=r.id, raw_text="alobal"),
        # builtin ne-surovina se seedovaným lookup_key – /unmatched ji nesmí ukazovat
        RecipeIngredient(recipe_id=r.id, raw_text="pečící papír"),
    ])
    db.commit()
    return {"recipe": r.id, "kure": kure.id, "tajna": tajna.id}


def run_tests() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal failed
        print(f"{'OK ' if cond else 'FAIL'} {name}{(' – ' + detail) if detail and not cond else ''}")
        if not cond:
            failed += 1

    with TestClient(app) as c:
        db = SessionLocal()
        ids = _seed(db)

        # ─── Hledání (SQLite → ILIKE fallback) ───────────────────────────
        r = c.get("/api/recipes", params={"q": "segedínský"})
        check("hledání najde recept (ILIKE fallback)",
              r.status_code == 200 and any(
                  it["id"] == ids["recipe"] for it in r.json()["items"]),
              str(r.json())[:200])

        # ─── Hledání přes skloňování (normalizovaný search_text) ─────────
        # Recept se uloží se stemovaným textem stejně jako při ingestu;
        # dotaz se normalizuje toutéž funkcí, takže se musí potkat i tvary,
        # které se nekryjí ani prefixem („péct" vs „pečeme").
        from app.modules.textnorm import refresh_search_text  # noqa: PLC0415

        r2 = Recipe(title="Pečené kuře na paprice", source_url="http://test/g2",
                    instructions="Kuře nakrájíme a pečeme v troubě 40 minut.")
        db.add(r2)
        db.flush()
        r2.ingredients.append(
            RecipeIngredient(raw_text="300 g kuřecích prsou")
        )
        refresh_search_text(r2)
        db.commit()

        for query, why in [
            ("péct", "infinitiv najde 'pečeme'"),
            ("pekli", "minulý čas najde 'pečeme'"),
            ("upéct", "předponový tvar najde 'pečeme'"),
            ("kuřecí prsa", "1. pád najde 'kuřecích prsou' ze surovin"),
            ("troubu", "4. pád najde 'v troubě'"),
            ("nakrajet", "bez diakritiky najde 'nakrájíme'"),
        ]:
            resp = c.get("/api/recipes", params={"q": query})
            check(f"hledání '{query}': {why}",
                  resp.status_code == 200 and any(
                      it["id"] == r2.id for it in resp.json()["items"]),
                  str(resp.json())[:160])

        resp = c.get("/api/recipes", params={"q": "čokoláda"})
        check("nesouvisející dotaz recept nenajde",
              all(it["id"] != r2.id for it in resp.json()["items"]))

        # ─── Spolehlivost výživy v detailu ───────────────────────────────
        r = c.get(f"/api/recipes/{ids['recipe']}")
        pct = r.json().get("nutrition_estimated_pct")
        check("detail nese nutrition_estimated_pct = 50 %", pct == 50, str(pct))

        # ─── Backfill: purge nadpisů + fuzzy match + alias s lookup_key ──
        out = backfill.backfill()
        # "alobal" (legacy) i "pečící papír" (builtin non-food) dostaly
        # příznak nonfood → nepočítají se mezi čekající; čekající = 0
        check("backfill: čekajících 0, ne-suroviny označené",
              out.get("rows_unmatched") == 0 and out.get("rows_nonfood") == 2,
              str({k: out.get(k) for k in ('rows_unmatched', 'rows_nonfood', 'error')}))
        db.expire_all()
        header = db.query(RecipeIngredient).filter_by(
            raw_text="Na vymazání a vysypání formy:").one_or_none()
        check("nadpis skupiny (5 slov) byl automaticky smazán", header is None)
        sep = db.query(RecipeIngredient).filter_by(raw_text="-----").one_or_none()
        check("ozdobný oddělovač '-----' byl automaticky smazán", sep is None)
        papir_alias = db.query(IngredientAlias).filter_by(
            lookup_key=make_lookup_key("pečící papír")).one_or_none()
        check("builtin ne-surovina 'pečící papír' je ve slovníku",
              papir_alias is not None and papir_alias.kind == "packaging"
              and papir_alias.source == "builtin",
              f"{papir_alias and papir_alias.kind}/{papir_alias and papir_alias.source}")
        # kolizní legacy alias přežil bez přepsání a start nespadl
        legacy = db.query(IngredientAlias).filter_by(alias="alobal").one_or_none()
        check("legacy alias 'alobal' seed nepřepsal ani neshodil start",
              legacy is not None and legacy.lookup_key is None
              and legacy.source != "builtin",
              f"{legacy and legacy.lookup_key}/{legacy and legacy.source}")
        row = db.query(RecipeIngredient).filter_by(raw_text="2 ks kuřecích prs").one()
        matched_name = db.get(Ingredient, row.ingredient_id).name_cs if row.ingredient_id else None
        # seed_starter obsahuje vlastní "kuřecí prsa" – stačí, že match míří na
        # surovinu s tímhle názvem (id se může lišit od té naší testovací)
        check("řádek míří na kuřecí prsa", matched_name == "kuřecí prsa", str(matched_name))
        alias = db.query(IngredientAlias).filter_by(
            lookup_key=make_lookup_key("2 ks kuřecích prs")).one_or_none()
        check("fuzzy alias má lookup_key (kompatibilní s llm_match)",
              alias is not None and alias.source == "import")

        # ─── Dostupnost vůči spíži (denormalizovaný ing_total) ───────────
        # backfill dopočítal ing_total (recompute + pojistný UPDATE);
        # výpis z něj žije místo agregace celé recipe_ingredient.
        db.expire_all()
        rec = db.get(Recipe, ids["recipe"])
        check("ing_total odpovídá napárovaným řádkům",
              rec.ing_total == 3, str(rec.ing_total))

        from app.models import PantryItem
        db.add_all([
            PantryItem(ingredient_id=ids["kure"]),
            PantryItem(ingredient_id=ids["tajna"]),
        ])
        db.commit()
        r = c.get("/api/recipes", params={"q": "segedínský"})
        it = next(x for x in r.json()["items"] if x["id"] == ids["recipe"])
        # 3 napárované řádky: kure (ve spíži), tajna (ve spíži) a fuzzy řádek
        # na seedované "kuřecí prsa" (jiné id, ve spíži není)
        check("karta: total=3, have=2, missing=1",
              it["total"] == 3 and it["have"] == 2 and it["missing_count"] == 1,
              str({k: it[k] for k in ("total", "have", "missing_count")}))
        r = c.get("/api/recipes", params={"q": "segedínský", "only_have": True})
        check("only_have recept s chybějící surovinou vyřadí",
              all(x["id"] != ids["recipe"] for x in r.json()["items"]))
        r = c.get("/api/recipes", params={"q": "segedínský", "max_missing": 1})
        check("max_missing=1 recept pustí (a count sedí)",
              any(x["id"] == ids["recipe"] for x in r.json()["items"])
              and r.json()["total"] >= 1, str(r.json()["total"]))

        # ─── /unmatched neukazuje už rozhodnuté ne-suroviny ──────────────
        r = c.get("/api/maintenance/unmatched")
        texts = [it["raw_text"] for it in r.json()["items"]]
        check("/unmatched neobsahuje označené ne-suroviny (pečící papír, alobal)",
              "pečící papír" not in texts and "alobal" not in texts, str(texts))

        # ─── Cookie přihlašování ─────────────────────────────────────────
        r = c.put("/api/admin/password", json={"password": "tajneheslo"})
        check("nastavení hesla projde", r.status_code == 200 and r.json()["auth_enabled"])

        r = c.get("/api/recipes")
        check("bez přihlášení 401", r.status_code == 401, str(r.status_code))

        r = c.post("/api/auth/login", json={"password": "spatne"})
        check("špatné heslo → 401", r.status_code == 401)

        r = c.post("/api/auth/login", json={"password": "tajneheslo"})
        check("login projde a nastaví cookie",
              r.status_code == 200 and COOKIE_NAME in c.cookies, str(dict(c.cookies)))

        # jen cookie, žádný Bearer header → musí projít
        r = c.get("/api/recipes")
        check("cookie sama o sobě stačí", r.status_code == 200, str(r.status_code))

        r = c.get("/api/auth/status")
        check("status vidí přihlášení přes cookie", r.json()["authenticated"] is True)

        r = c.post("/api/auth/logout")
        check("logout smaže cookie",
              r.status_code == 200 and not c.cookies.get(COOKIE_NAME))
        r = c.get("/api/recipes")
        check("po odhlášení zase 401", r.status_code == 401, str(r.status_code))

        # Bearer token cesta (localStorage klienti) musí fungovat dál
        r = c.post("/api/auth/login", json={"password": "tajneheslo"})
        token = r.json()["token"]
        c.cookies.clear()
        r = c.get("/api/recipes", headers={"Authorization": f"Bearer {token}"})
        check("Bearer token funguje dál", r.status_code == 200, str(r.status_code))

        db.close()

    print(f"\n{'='*60}\n{'FAILED: ' + str(failed) if failed else 'ALL PASS'}\n")
    return failed


if __name__ == "__main__":
    sys.exit(run_tests())
