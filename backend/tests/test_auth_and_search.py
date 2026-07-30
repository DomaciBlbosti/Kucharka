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
        # builtin ne-surovina – zůstane nenapárovaná, ale slovník ji zná
        RecipeIngredient(recipe_id=r.id, raw_text="alobal"),
    ])
    db.commit()
    return {"recipe": r.id, "kure": kure.id}


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

        # ─── Spolehlivost výživy v detailu ───────────────────────────────
        r = c.get(f"/api/recipes/{ids['recipe']}")
        pct = r.json().get("nutrition_estimated_pct")
        check("detail nese nutrition_estimated_pct = 50 %", pct == 50, str(pct))

        # ─── Backfill: purge nadpisů + fuzzy match + alias s lookup_key ──
        out = backfill.backfill()
        # zbývá jen "alobal" (builtin non-food – zůstává bez suroviny záměrně)
        check("backfill napároval fuzzy řádek", out.get("rows_unmatched") == 1,
              str({k: out.get(k) for k in ('rows_unmatched', 'error')}))
        db.expire_all()
        header = db.query(RecipeIngredient).filter_by(
            raw_text="Na vymazání a vysypání formy:").one_or_none()
        check("nadpis skupiny (5 slov) byl automaticky smazán", header is None)
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
