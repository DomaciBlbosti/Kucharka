"""Testy ukotvení slovníku surovin na NutriDatabázi a hledání duplicit.

Regresní scénář z produkce: 12 059 surovin v databázi, což je na domácí
kuchařku podezřele moc. Příčiny byly dvě a obě jsou tu pokryté:
  * `normalizer.create_ingredient_via_llm` zapisoval název vymyšlený LLM bez
    jakéhokoli lookupu (fuzzy match předtím běžel na SUROVÝ text),
  * `llm_match.get_or_create_ingredient` porovnával jen přesnou shodu názvu.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-ingredients-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

import app.models  # noqa: E402,F401 - naplní metadata před create_all
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Ingredient, PantryItem, Recipe, RecipeIngredient  # noqa: E402
from app.modules import ingredient_audit, ingredient_resolve, llm_match  # noqa: E402

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
    db = SessionLocal()
    try:
        # NutriDatabáze = referenční záznamy, ollama = odhady
        nut_prsa = Ingredient(name_cs="kuřecí prsa", source="nutridatabaze", kcal_100g=110)
        nut_paprika = Ingredient(name_cs="paprika mletá sladká",
                                 source="nutridatabaze", kcal_100g=282)
        ollama_prsa = Ingredient(name_cs="Kuřecí prsa", source="ollama", kcal_100g=120)
        smetana = Ingredient(name_cs="smetana ke šlehání", source="nutridatabaze")
        zakysana = Ingredient(name_cs="zakysaná smetana", source="nutridatabaze")
        db.add_all([nut_prsa, nut_paprika, ollama_prsa, smetana, zakysana])
        db.commit()
        ingredient_resolve.invalidate()

        # ── normalizovaný klíč nezávisí na pořadí slov ani na skloňování ──
        nk = ingredient_resolve.name_key
        check("klíč nezávisí na pořadí slov",
              nk("paprika mletá sladká") == nk("mletá sladká paprika"),
              f"{nk('paprika mletá sladká')} vs {nk('mletá sladká paprika')}")
        check("klíč nezávisí na skloňování",
              nk("kuřecí prsa") == nk("kuřecích prsou"),
              f"{nk('kuřecí prsa')} vs {nk('kuřecích prsou')}")
        check("různé suroviny mají různý klíč",
              nk("smetana ke šlehání") != nk("zakysaná smetana"))

        # ── hledání existující suroviny ──
        find = ingredient_resolve.find_by_name
        check("přesná shoda najde", find(db, "paprika mletá sladká").id == nut_paprika.id)
        check("jiné pořadí slov najde totéž",
              find(db, "mletá sladká paprika").id == nut_paprika.id)
        check("jiný pád najde totéž",
              (find(db, "kuřecích prsou") or Ingredient()).id in
              (nut_prsa.id, ollama_prsa.id))
        check("při shodě vyhraje referenční z NutriDatabáze",
              find(db, "kuřecí prsa").id == nut_prsa.id,
              str(find(db, "kuřecí prsa").source))
        check("neznámá surovina se nenajde", find(db, "wasabi pasta") is None)
        check("prázdný vstup se nenajde", find(db, "") is None and find(db, None) is None)
        check("smetana se nespojí se zakysanou",
              find(db, "zakysaná smetana").id == zakysana.id)

        # ── get_or_create nezakládá duplicity ──
        before = db.query(Ingredient).count()
        again = ingredient_resolve.get_or_create(db, "Kuřecích prsou")
        db.commit()
        check("existující surovina se nezaloží znovu",
              db.query(Ingredient).count() == before, str(db.query(Ingredient).count()))
        check("vrátí se referenční záznam", again.source == "nutridatabaze", again.source)

        nova = ingredient_resolve.get_or_create(db, "wasabi pasta")
        db.commit()
        check("opravdu nová surovina vznikne",
              db.query(Ingredient).count() == before + 1 and nova.name_cs == "wasabi pasta")
        check("nová má zdroj ollama", nova.source == "ollama")

        # llm_match používá stejnou cestu (dřív jen přesná shoda názvu)
        before = db.query(Ingredient).count()
        ing = llm_match.get_or_create_ingredient(db, "kuřecí prso")
        db.commit()
        check("llm_match nezaloží variantu existující suroviny",
              db.query(Ingredient).count() == before, str(ing.name_cs))

        # ── prompt označí referenční záznamy hvězdičkou ──
        prompt = llm_match._make_prompt(
            [(nut_prsa.id, "kuřecí prsa"), (ollama_prsa.id, "Kuřecí prsa")],
            ["chicken breast"],
            nutridb_ids={nut_prsa.id},
        )
        check("referenční záznam má v promptu *",
              f"{nut_prsa.id}: kuřecí prsa *" in prompt, prompt[-300:])
        check("odhad hvězdičku nemá",
              f"{ollama_prsa.id}: Kuřecí prsa\n" in prompt, prompt[-300:])
        check("prompt vysvětluje, co * znamená", "NutriDatabáze" in prompt)

        # ── audit shluky najde a navrhne, co nechat ──
        r = Recipe(title="Test", source_url="http://t/1")
        db.add(r)
        db.flush()
        db.add_all([
            RecipeIngredient(recipe_id=r.id, raw_text="kuřecí prsa",
                             ingredient_id=ollama_prsa.id),
            RecipeIngredient(recipe_id=r.id, raw_text="paprika",
                             ingredient_id=nut_paprika.id),
        ])
        db.add(PantryItem(ingredient_id=nut_prsa.id))
        db.commit()

        out = ingredient_audit.run()
        report = __import__("json").loads(
            ingredient_audit.REPORT_PATH.read_text(encoding="utf-8")
        )
        check("audit spočítal všechny suroviny",
              report["total_ingredients"] == db.query(Ingredient).count(),
              str(report["total_ingredients"]))
        check("rozpad podle zdroje sedí",
              report["by_source"].get("nutridatabaze") == 4, str(report["by_source"]))
        check("našel se shluk kuřecích prsou", report["clusters_total"] >= 1)

        prsa = [c for c in report["clusters"]
                if any(m["id"] in (nut_prsa.id, ollama_prsa.id) for m in c["members"])]
        check("obě varianty prsou jsou v jednom shluku",
              prsa and prsa[0]["size"] == 2, str(prsa[:1])[:200])
        check("shluk navrhuje nechat referenční záznam",
              prsa and prsa[0]["suggested_keep"] == nut_prsa.id,
              str(prsa[0]["suggested_keep"]) if prsa else "")
        check("shluk nese využití (recepty, spíž)",
              prsa and any(m["recipes"] == 1 for m in prsa[0]["members"])
              and any(m["pantry"] == 1 for m in prsa[0]["members"]),
              str(prsa[0]["members"]) if prsa else "")
        check("smetany se do jednoho shluku nespojily",
              not any({m["id"] for m in c["members"]} == {smetana.id, zakysana.id}
                      for c in report["clusters"]))
        check("hlásí se počet duplicitních řádků",
              report["duplicate_rows"] >= 1, str(report["duplicate_rows"]))
        check("běh vrátí cestu k reportu", "report_path" in out)
        check("audit nic nesmazal",
              db.query(Ingredient).count() == report["total_ingredients"])
    finally:
        db.close()

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
