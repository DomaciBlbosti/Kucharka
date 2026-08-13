"""Testy dávkového párování (llm_match) + katalogu rozhodnutí (match_decision).

Běží nad SQLite bez sítě – LLM volání se mockuje. Spuštění (z backend/):
    python -m tests.test_llm_match  (nebo pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# DB musí být nastavená PŘED importem app.db
_tmpdir = tempfile.mkdtemp(prefix="kucharka-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Ingredient, IngredientAlias, MatchDecision, Recipe, RecipeIngredient,
)
from app.modules import llm_match  # noqa: E402
from app.modules.lookup import make_lookup_key  # noqa: E402


# ─── Mock LLM ────────────────────────────────────────────────────────────────

class FakeLLM:
    """Náhrada llmclient: vrací připravené odpovědi a počítá volání."""

    def __init__(self):
        self.responses: list = []
        self.calls = 0

    def structured_json(self, prompt, **kw):
        self.calls += 1
        if self.responses:
            resp = self.responses.pop(0)
            # None ve frontě = simulovaný timeout (jako vyčerpaná fronta)
            self._err = None if resp is not None else "test: Ollama timeout po 300s"
            return resp
        self._err = "test: Ollama timeout po 300s"
        return None

    def last_error(self):
        return getattr(self, "_err", None)

    def availability_error(self):
        return None

    def is_available(self):
        return True

    def active_model(self, ollama_model=None):
        return "fake-model"


fake = FakeLLM()
llm_match.llmclient = fake  # modul má llmclient importnutý jako atribut


def _seed(db) -> dict:
    """Základní data: suroviny + recept se 4 nenapárovanými řádky."""
    kure = Ingredient(name_cs="kuřecí prsa", kcal_100g=110, source="test")
    mouka = Ingredient(name_cs="mouka hladká", kcal_100g=350, source="test")
    db.add_all([kure, mouka])
    db.flush()
    r = Recipe(title="Testovací recept", source_url="http://test/1", servings=4)
    db.add(r)
    db.flush()
    rows = [
        RecipeIngredient(recipe_id=r.id, raw_text="500 g chicken breast"),   # jistý match
        RecipeIngredient(recipe_id=r.id, raw_text="2 cups plain flour"),     # nejistý → návrh
        RecipeIngredient(recipe_id=r.id, raw_text="silikonová forma na pečení"),  # non-food
        RecipeIngredient(recipe_id=r.id, raw_text="polárkový dort z Marsu"), # bez shody
    ]
    db.add_all(rows)
    db.commit()
    return {"kure": kure.id, "mouka": mouka.id, "recipe": r.id}


def run_tests() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal failed
        print(f"{'OK ' if cond else 'FAIL'} {name}{(' – ' + detail) if detail and not cond else ''}")
        if not cond:
            failed += 1

    Base.metadata.create_all(engine)
    db = SessionLocal()
    ids = _seed(db)

    settings.llm_match_enabled = True
    settings.llm_match_min_confidence = 0.7
    settings.llm_match_failure_pause_s = 0  # ať test nečeká na "zotavení GPU"
    settings.llm_match_batch_pause_s = 0

    # ─── Běh 1: LLM odpoví na všechny 4 položky (podle indexu) ───────────
    # Pořadí položek v dávce odpovídá pořadí vkládání (unikátní lookup_key,
    # dict drží insertion order).
    fake.responses = [{
        "items": [
            {"i": 0, "ingredient_id": ids["kure"], "category": "food", "confidence": 0.95},
            {"i": 1, "ingredient_id": ids["mouka"], "category": "food", "confidence": 0.5},
            {"i": 2, "ingredient_id": None, "category": "equipment", "confidence": 0.9},
            {"i": 3, "ingredient_id": None, "category": "food", "confidence": 0.2},
        ]
    }]
    fake.calls = 0
    out = llm_match.process_batch()
    # 1 dávkové volání + 1 pokus kontextové fáze o čerstvý no_match (selže,
    # fronta odpovědí je prázdná – to je v pořádku, zkusí se příště)
    check("běh 1: 1 dávkové + 1 kontextové volání", fake.calls == 2, str(fake.calls))
    check("běh 1: applied=1", out.get("applied") == 1, str(out))
    check("běh 1: suggested=1", out.get("suggested") == 1, str(out))
    check("běh 1: nonfood=1", out.get("nonfood") == 1, str(out))
    check("běh 1: no_match=1", out.get("no_match") == 1, str(out))

    db.expire_all()
    row_kure = db.query(RecipeIngredient).filter_by(raw_text="500 g chicken breast").one()
    check("řádek s jistým matchem je napárovaný", row_kure.ingredient_id == ids["kure"])
    check("řádek má dopočtené gramy a kcal",
          row_kure.grams == 500 and row_kure.kcal == 550,
          f"grams={row_kure.grams} kcal={row_kure.kcal}")
    row_flour = db.query(RecipeIngredient).filter_by(raw_text="2 cups plain flour").one()
    check("nejistý match zůstal nenapárovaný (jen návrh)", row_flour.ingredient_id is None)

    recipe = db.get(Recipe, ids["recipe"])
    check("recept má přepočtené kcal/porci", (recipe.kcal_per_serving or 0) > 0,
          str(recipe.kcal_per_serving))

    decs = {d.lookup_key: d for d in db.query(MatchDecision).all()}
    check("katalog má 4 rozhodnutí", len(decs) == 4, str(list(decs)))
    d_sugg = decs.get(make_lookup_key("2 cups plain flour"))
    check("návrh nese surovinu a confidence",
          d_sugg is not None and d_sugg.status == "suggested"
          and d_sugg.ingredient_id == ids["mouka"] and d_sugg.confidence == 0.5)

    alias_kure = db.query(IngredientAlias).filter_by(
        lookup_key=make_lookup_key("500 g chicken breast")).one_or_none()
    check("alias pro jistý match existuje (source=llm)",
          alias_kure is not None and alias_kure.source == "llm"
          and alias_kure.ingredient_id == ids["kure"])

    # označ no_match jako "kontextem už prošlé", ať další testy mají
    # deterministické počty volání (kontextová fáze cílí na ctx_tried=False)
    db.query(MatchDecision).filter_by(status="no_match").update({"ctx_tried": True})
    db.commit()

    # ─── Běh 2: nic nového → žádné LLM volání ────────────────────────────
    fake.responses = []
    fake.calls = 0
    out2 = llm_match.process_batch()
    check("běh 2: rozhodnuté položky se znovu neptají (0 volání)", fake.calls == 0,
          f"calls={fake.calls} out={out2}")

    # ─── Slovníkový sweep: nový řádek se stejným textem, bez LLM ─────────
    db.add(RecipeIngredient(recipe_id=ids["recipe"], raw_text="300 g chicken breast"))
    db.commit()
    fake.calls = 0
    out3 = llm_match.process_batch()
    check("sweep: nový řádek napárován slovníkem bez LLM",
          out3.get("dict_applied", 0) >= 1 and fake.calls == 0, str(out3))
    row_new = db.query(RecipeIngredient).filter_by(raw_text="300 g chicken breast").one()
    db.refresh(row_new)
    check("sweep: řádek má surovinu i kcal",
          row_new.ingredient_id == ids["kure"] and row_new.kcal == 330,
          f"ing={row_new.ingredient_id} kcal={row_new.kcal}")

    # ─── Chybová dávka: error + attempts, retry, pak strop ───────────────
    db.add(RecipeIngredient(recipe_id=ids["recipe"], raw_text="1 ks flux kondenzátor"))
    db.commit()
    fake.responses = []  # LLM vrací None
    for attempt in range(1, llm_match.MAX_ATTEMPTS + 2):
        fake.calls = 0
        llm_match.process_batch()
    d_err = db.query(MatchDecision).filter_by(
        lookup_key=make_lookup_key("1 ks flux kondenzátor")).one_or_none()
    check("chybová položka je v katalogu jako error s attempts=MAX",
          d_err is not None and d_err.status == "error"
          and d_err.attempts == llm_match.MAX_ATTEMPTS,
          f"{d_err.status if d_err else None}/{d_err.attempts if d_err else None}")
    # dávková fáze už položku nezkouší; kontextová fáze ji ale ještě zkusí
    # (druhá šance přes celý recept) – proto 1 volání, ne 0
    check("po stropu zbývá jen kontextový pokus", fake.calls == 1, str(fake.calls))

    # ─── Ruční dořešení návrhu (accept) ──────────────────────────────────
    from app.routers.maintenance import DecisionResolve, resolve_decision

    res = resolve_decision(d_sugg.id, DecisionResolve(action="accept"), db)
    check("accept návrhu projde", res.get("ok") is True, str(res))
    db.expire_all()
    row_flour = db.query(RecipeIngredient).filter_by(raw_text="2 cups plain flour").one()
    check("accept: řádek je napárovaný", row_flour.ingredient_id == ids["mouka"])
    d_sugg = db.get(MatchDecision, d_sugg.id)
    check("accept: rozhodnutí je applied/manual",
          d_sugg.status == "applied" and d_sugg.model == "manual")
    alias_flour = db.query(IngredientAlias).filter_by(
        lookup_key=make_lookup_key("2 cups plain flour")).one_or_none()
    check("accept: alias je verified/manual",
          alias_flour is not None and alias_flour.verified and alias_flour.source == "manual")

    # ─── Retry: smaže rozhodnutí → příští běh se ptá znovu ───────────────
    d_nm = db.query(MatchDecision).filter_by(status="no_match").first()
    res = resolve_decision(d_nm.id, DecisionResolve(action="retry"), db)
    check("retry smaže rozhodnutí", res.get("ok") is True
          and db.get(MatchDecision, d_nm.id) is None)
    fake.responses = [{"items": []}]  # model vrátí prázdno → error záznam
    fake.calls = 0
    llm_match.process_batch()
    # 1 dávkové volání + 1 kontextový pokus o čerstvé error záznamy
    check("retry: položka se znovu poslala do LLM", fake.calls == 2, str(fake.calls))

    # ─── Nonfood + ignore akce ───────────────────────────────────────────
    d_err = db.query(MatchDecision).filter_by(
        lookup_key=make_lookup_key("1 ks flux kondenzátor")).one()
    res = resolve_decision(d_err.id, DecisionResolve(action="ignore"), db)
    check("ignore nastaví status ignored", res["decision"]["status"] == "ignored")

    # ─── Nová surovina: auto-vytvoření (auto_ingredients=True) ───────────
    # nejdřív uklidit zbylý error záznam, ať je fronta deterministická
    for d in db.query(MatchDecision).filter_by(status="error").all():
        resolve_decision(d.id, DecisionResolve(action="ignore"), db)
    settings.auto_ingredients = True
    db.add(RecipeIngredient(recipe_id=ids["recipe"], raw_text="1 balíček ztužovače šlehačky"))
    db.commit()
    fake.responses = [
        {"items": [{"i": 0, "ingredient_id": None, "name_cs": "ztužovač šlehačky",
                    "category": "food", "confidence": 0.9}]},
        # druhé volání = dávkový odhad výživy nové suroviny
        {"items": [{"i": 0, "kcal_100g": 380, "protein_100g": 0.5, "carbs_100g": 90,
                    "fat_100g": 0.5, "density": None, "category": "ostatní"}]},
    ]
    fake.calls = 0
    out = llm_match.process_batch()
    check("auto-create: applied=1, created=1",
          out.get("applied") == 1 and out.get("created") == 1, str(out))
    from sqlalchemy import func as _f
    ing_new = db.query(Ingredient).filter(
        _f.lower(Ingredient.name_cs) == "ztužovač šlehačky").one_or_none()
    check("auto-create: surovina existuje s odhadnutou výživou (source=ollama)",
          ing_new is not None and ing_new.source == "ollama" and ing_new.kcal_100g == 380,
          f"{ing_new and ing_new.source}/{ing_new and ing_new.kcal_100g}")
    row_new2 = db.query(RecipeIngredient).filter_by(
        raw_text="1 balíček ztužovače šlehačky").one()
    db.refresh(row_new2)
    check("auto-create: řádek je napárovaný", row_new2.ingredient_id == (ing_new.id if ing_new else None))

    # ─── Nová surovina jako návrh (auto_ingredients=False) + accept ──────
    settings.auto_ingredients = False
    db.add(RecipeIngredient(recipe_id=ids["recipe"], raw_text="2 lžičky Glutasolu"))
    db.commit()
    fake.responses = [
        {"items": [{"i": 0, "ingredient_id": None, "name_cs": "glutasol",
                    "category": "food", "confidence": 0.85}]},
    ]
    llm_match.process_batch()
    d_glut = db.query(MatchDecision).filter_by(
        lookup_key=make_lookup_key("2 lžičky Glutasolu")).one_or_none()
    check("bez auto_ingredients: návrh na založení v katalogu",
          d_glut is not None and d_glut.status == "suggested"
          and d_glut.suggested_name == "glutasol" and d_glut.ingredient_id is None,
          f"{d_glut and d_glut.status}/{d_glut and d_glut.suggested_name}")
    fake.responses = [
        {"items": [{"i": 0, "kcal_100g": 250, "protein_100g": 10, "carbs_100g": 40,
                    "fat_100g": 2, "density": None, "category": "koření"}]},
    ]
    res = resolve_decision(d_glut.id, DecisionResolve(action="accept"), db)
    check("accept návrhu založí surovinu a napáruje", res.get("ok") is True
          and res.get("rows") == 1, str(res))
    ing_glut = db.query(Ingredient).filter(
        _f.lower(Ingredient.name_cs) == "glutasol").one_or_none()
    check("založená surovina má odhadnutou výživu",
          ing_glut is not None and ing_glut.kcal_100g == 250,
          str(ing_glut and ing_glut.kcal_100g))

    # ─── Auto-půlení dávky při timeoutu ──────────────────────────────────
    # 4 nové položky v jedné dávce: první volání vyprší (timeout), poloviny
    # (2+2) už projdou → všechno napárované, žádná chyba.
    half_items = ["chia semínka bio", "quinoa červená", "kokosový cukr raw",
                  "psyllium vláknina"]
    for t in half_items:
        db.add(RecipeIngredient(recipe_id=ids["recipe"], raw_text=t))
    db.commit()
    settings.auto_ingredients = True
    fake.responses = [
        None,  # celá dávka: timeout
        {"items": [
            {"i": 0, "ingredient_id": None, "name_cs": "chia semínka", "category": "food", "confidence": 0.9},
            {"i": 1, "ingredient_id": None, "name_cs": "quinoa", "category": "food", "confidence": 0.9},
        ]},
        {"items": [
            {"i": 0, "ingredient_id": None, "name_cs": "kokosový cukr", "category": "food", "confidence": 0.9},
            {"i": 1, "ingredient_id": None, "name_cs": "psyllium", "category": "food", "confidence": 0.9},
        ]},
        {"items": []},  # dávkový odhad výživy (prázdný = bez výživy, nevadí)
    ]
    fake.calls = 0
    out_half = llm_match.process_batch(batch_size=4)
    check("timeout dávky → automatické půlení → vše napárováno",
          out_half.get("applied") == 4 and out_half.get("errors") == 0
          and "aborted" not in out_half,
          f"calls={fake.calls} out={out_half}")

    # ─── Circuit breaker: 5 selhaných dávek v řadě zastaví běh ───────────
    for i in range(8):
        db.add(RecipeIngredient(recipe_id=ids["recipe"], raw_text=f"exotická surovina {chr(97 + i)}"))
    db.commit()
    fake.responses = []  # všechna volání selžou
    fake.calls = 0
    out_cb = llm_match.process_batch(batch_size=1)
    check("circuit breaker: běh se zastavil po 5 selhaných dávkách",
          fake.calls == llm_match.MAX_CONSECUTIVE_BATCH_FAILURES
          and "aborted" in out_cb,
          f"calls={fake.calls} out={out_cb}")
    d_cb = db.query(MatchDecision).filter_by(status="error").first()
    check("chybové rozhodnutí nese skutečnou příčinu",
          d_cb is not None and "Ollama timeout" in (d_cb.error or ""),
          str(d_cb and d_cb.error))
    check("stav běhu nese poslední chybu",
          "Ollama timeout" in (llm_match.status().get("last_error") or ""))
    # úklid pro další testy: chybové položky ignorovat
    from app.routers.maintenance import DecisionResolve as _DR, resolve_decision as _rd
    for d in db.query(MatchDecision).filter_by(status="error").all():
        _rd(d.id, _DR(action="ignore"), db)

    # ─── Prompt nese kontext receptu ─────────────────────────────────────
    p = llm_match._make_prompt([(1, "bazalka")], ["čerstvá bazalka"],
                               contexts=["Cannelloni s boloňskou omáčkou"])
    check("prompt obsahuje název receptu jako kontext",
          "recept: Cannelloni s boloňskou omáčkou" in p)
    p2 = llm_match._make_prompt([(1, "bazalka")], ["čerstvá bazalka"])
    check("prompt bez kontextu funguje dál", "0: čerstvá bazalka" in p2)

    # ─── Migrace: jednorázové znovuotevření starých no_match ─────────────
    from app import migrations
    from app.db import engine as _engine

    db.add(MatchDecision(lookup_key="stary-klic", sample_text="starý text",
                         status="no_match", attempts=0, occurrences=1))
    db.commit()
    migrations.run_all(_engine)
    db.expire_all()
    check("migrace smazala staré no_match bez návrhu",
          db.query(MatchDecision).filter_by(lookup_key="stary-klic").one_or_none() is None)
    db.add(MatchDecision(lookup_key="novy-klic", sample_text="nový text",
                         status="no_match", attempts=0, occurrences=1))
    db.commit()
    migrations.run_all(_engine)
    db.expire_all()
    check("migrace je jednorázová (marker) – nové no_match nechává",
          db.query(MatchDecision).filter_by(lookup_key="novy-klic").one_or_none() is not None)

    # ─── Fáze 3: kontextové dořešení po receptech ────────────────────────
    # úklid zbytků z circuit-breaker testu (3 exotické řádky bez rozhodnutí
    # by jinak vstoupily do dávkové fronty a posunuly počty volání)
    for ri in db.query(RecipeIngredient).filter(
            RecipeIngredient.raw_text.like("exotická surovina%")).all():
        db.delete(ri)
    db.commit()

    r2 = Recipe(title="Babiččin jablečný závin", source_url="http://test/2",
                servings=6, instructions="Těsto rozválíme, poklademe jablky a pečeme.")
    db.add(r2)
    db.flush()
    db.add_all([
        RecipeIngredient(recipe_id=r2.id, raw_text="hrst čerstvé máty na dozdobení dortu"),
        RecipeIngredient(recipe_id=r2.id, raw_text="dle chuti dosolíme a opepříme"),
    ])
    db.commit()
    key_mata = make_lookup_key("hrst čerstvé máty na dozdobení dortu")
    key_note = make_lookup_key("dle chuti dosolíme a opepříme")
    # simulace: dávková fáze je už dřív vyhodnotila jako 'bez shody'
    llm_match._upsert_decision(db, key_mata, "hrst čerstvé máty na dozdobení dortu",
                               status="no_match", category="food", occurrences=1)
    llm_match._upsert_decision(db, key_note, "dle chuti dosolíme a opepříme",
                               status="no_match", category="food", occurrences=1)
    db.commit()
    settings.auto_ingredients = True
    fake.responses = [
        # kontextové volání (pořadí = pořadí řádků v receptu)
        {"items": [
            {"i": 0, "verdict": "ingredient", "name_cs": "máta", "confidence": 0.9},
            {"i": 1, "verdict": "note", "confidence": 0.95},
        ]},
        # odhad výživy nové suroviny
        {"items": [{"i": 0, "kcal_100g": 44, "protein_100g": 3.3, "carbs_100g": 8,
                    "fat_100g": 0.7, "density": None, "category": "bylinky"}]},
    ]
    fake.calls = 0
    out_ctx = llm_match.process_batch()
    check("kontextová fáze: surovina dořešena, poznámka smazána",
          out_ctx.get("ctx_applied") == 1 and out_ctx.get("ctx_removed") == 1,
          f"calls={fake.calls} out={out_ctx}")
    db.expire_all()
    row_mata = db.query(RecipeIngredient).filter_by(
        raw_text="hrst čerstvé máty na dozdobení dortu").one()
    ing_mata = db.get(Ingredient, row_mata.ingredient_id) if row_mata.ingredient_id else None
    check("kontext: řádek napárovaný na novou surovinu 'máta' s výživou",
          ing_mata is not None and ing_mata.name_cs == "máta" and ing_mata.kcal_100g == 44,
          f"{ing_mata and ing_mata.name_cs}/{ing_mata and ing_mata.kcal_100g}")
    note_row = db.query(RecipeIngredient).filter_by(
        raw_text="dle chuti dosolíme a opepříme").one_or_none()
    check("kontext: poznámka smazána z receptu", note_row is None)
    d_note = db.query(MatchDecision).filter_by(lookup_key=key_note).one()
    check("kontext: rozhodnutí poznámky je ignored s vysvětlením",
          d_note.status == "ignored" and "poznámka" in (d_note.error or ""),
          f"{d_note.status}/{d_note.error}")
    d_mata = db.query(MatchDecision).filter_by(lookup_key=key_mata).one()
    check("kontext: rozhodnutí máty je applied a neopakuje se",
          d_mata.status == "applied" and d_mata.ctx_tried is True)
    # druhý běh: nic dalšího k dotazování → žádné LLM volání
    fake.responses = []
    fake.calls = 0
    llm_match.process_batch()
    check("kontext: druhý běh se už neptá", fake.calls == 0, str(fake.calls))

    # ─── Retry ne-suroviny smaže i neověřený LLM alias ───────────────────
    d_forma = db.query(MatchDecision).filter_by(
        lookup_key=make_lookup_key("silikonová forma na pečení")).one()
    check("prekondice: forma je nonfood s llm aliasem",
          d_forma.status == "nonfood" and db.query(IngredientAlias).filter_by(
              lookup_key=d_forma.lookup_key).one().source == "llm")
    resolve_decision(d_forma.id, DecisionResolve(action="retry"), db)
    alias_after = db.query(IngredientAlias).filter_by(
        lookup_key=make_lookup_key("silikonová forma na pečení")).one_or_none()
    check("retry ne-suroviny smaže rozhodnutí i neověřený alias",
          alias_after is None
          and db.query(MatchDecision).filter_by(
              lookup_key=make_lookup_key("silikonová forma na pečení")).one_or_none() is None)
    # → příští běh se na formu zeptá znovu (sweep ji už předem nerozhodne)

    # ─── Přehled endpointu decisions ─────────────────────────────────────
    from app.routers.maintenance import list_decisions

    listing = list_decisions(status="review", q="", limit=50, offset=0, db=db)
    check("listing decisions vrací summary",
          isinstance(listing.get("summary"), dict) and listing["summary"].get("applied", 0) >= 2,
          str(listing.get("summary")))

    db.close()
    print(f"\n{'='*60}\n{'FAILED: ' + str(failed) if failed else 'ALL PASS'}\n")
    return failed


if __name__ == "__main__":
    sys.exit(run_tests())
