"""Testy read-only auditu korpusu (profil + stratifikovaný vzorek).

Akceptační kritéria ze zadání: součty bucketů = total_recipes, žádné
duplicitní id ve vzorku, determinismus se stejným seedem, DB beze změny.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-audit-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Ingredient, Recipe, RecipeIngredient  # noqa: E402
from app.modules import corpus_audit  # noqa: E402

Base.metadata.create_all(engine)

# výstupy do dočasného adresáře, ne do repa
corpus_audit.ANALYSIS_DIR = Path(_tmpdir) / "analysis"
corpus_audit.PROFILE_PATH = corpus_audit.ANALYSIS_DIR / "corpus_profile.json"
corpus_audit.SAMPLE_PATH = corpus_audit.ANALYSIS_DIR / "corpus_sample.jsonl"

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


def seed_db():
    db = SessionLocal()
    ing = Ingredient(name_cs="mouka hladká", kcal_100g=350)
    db.add(ing)
    db.flush()
    proper = (
        "Mouku smícháme s mlékem a vejci. Těsto necháme 30 minut odpočinout.\n\n"
        "Palačinky smažíme na pánvi dozlatova, pečeme při 180 °C 10 minut."
    )
    for i in range(40):
        r = Recipe(
            title=f"Palačinky č. {i}" if i % 4 else "Palačinky",  # duplicitní názvy
            source_url=f"https://web{i % 3}.cz/r/{i}",
            source_domain=f"web{i % 3}.cz",
            instructions=proper if i % 5 else "Nasypeme a zamícháme.",  # část krátkých
        )
        db.add(r)
        db.flush()
        n_ing = 1 if i % 6 == 0 else 5  # část s ≤3 surovinami
        for j in range(n_ing):
            db.add(RecipeIngredient(
                recipe_id=r.id, raw_text=f"{100 + j} g mouka hladká",
                ingredient_id=ing.id,
            ))
    # recept z fotky: bez domény, bez postupu, bez surovin
    db.add(Recipe(title="Foto recept", source_url="photo://x", source_domain=None))
    db.commit()
    db.close()


def db_fingerprint():
    db = SessionLocal()
    try:
        recipes = db.query(Recipe).order_by(Recipe.id).all()
        rows = db.query(RecipeIngredient).order_by(RecipeIngredient.id).all()
        return (
            [(r.id, r.title, r.instructions, r.source_domain) for r in recipes],
            [(x.id, x.raw_text, x.ingredient_id) for x in rows],
        )
    finally:
        db.close()


def main():
    seed_db()
    before = db_fingerprint()

    out = corpus_audit.run(seed=42)
    check("běh vrátí cesty k oběma souborům",
          "profile_path" in out and "sample_path" in out)

    # ── profil ──
    profile = json.loads(corpus_audit.PROFILE_PATH.read_text(encoding="utf-8"))
    total = profile["total_recipes"]
    check("total_recipes sedí", total == 41, str(total))
    for metric in ("n_ingredients", "n_steps", "instr_chars", "n_cook_verbs", "ingr_coverage"):
        s = sum(profile["global"][metric].values())
        check(f"buckety {metric} dají total", s == total, f"{s} != {total}")
    check("empty_ingr zachytil foto recept", profile["global"]["has_empty_ingr"] == 1)
    check("has_time > 0 (30 minut v postupu)", profile["global"]["has_time"] > 0)
    doms = {d["domain"] for d in profile["by_domain"]}
    check("domény v rozpadu (bez None)", doms == {"web0.cz", "web1.cz", "web2.cz"}, str(doms))
    dup = profile["duplicate_titles"]
    check("duplicitní názvy: cluster 'palacinky'",
          dup["titles_with_2plus"] >= 1 and dup["max_cluster_size"] >= 10,
          str(dup))

    # ── vzorek ──
    lines = corpus_audit.SAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    rows = [json.loads(ln) for ln in lines[1:]]
    check("hlavička nese seed", header.get("seed") == 42)
    check("počet řádků = header.count", header.get("count") == len(rows))
    ids = [r["id"] for r in rows]
    check("žádné id dvakrát", len(ids) == len(set(ids)))
    check("řádky mají strata + metriky",
          all(r["strata"] and "matched_stems" in r["metrics"] for r in rows))
    multi = [r for r in rows if len(r["strata"]) > 1]
    check("deduplikace napříč vrstvami (víc strat na řádku existuje)",
          len(multi) > 0)
    short = [r for r in rows if "short_instr" in r["strata"]]
    check("short_instr vrstva obsazená a sedí s metrikou",
          short and all(r["metrics"]["instr_chars"] < 300 for r in short))
    # kmeny: pořádný postup má smaz/pec/mich…, krátký nemá nic
    proper_rows = [r for r in rows if r["metrics"]["instr_chars"] > 100]
    check("matched_stems v pořádném postupu",
          proper_rows and all("smaz" in r["metrics"]["matched_stems"] for r in proper_rows))
    check("'30 sekund' nespustí kmen 'sek' (sekund guard)",
          corpus_audit.matched_stems(corpus_audit.norm("počkejte 30 sekund")) == [])
    check("'nasekáme' kmen 'sek' spustí",
          "sek" in corpus_audit.matched_stems(corpus_audit.norm("nasekáme cibuli"))
          or corpus_audit.matched_stems(corpus_audit.norm("sekáme cibuli")) == ["sek"])

    # ── determinismus ──
    out2 = corpus_audit.run(seed=42)
    rows2 = [json.loads(ln) for ln in
             corpus_audit.SAMPLE_PATH.read_text(encoding="utf-8").splitlines()[1:]]
    check("stejný seed → stejná množina id",
          {r["id"] for r in rows2} == set(ids))
    corpus_audit.run(seed=7)
    rows3 = [json.loads(ln) for ln in
             corpus_audit.SAMPLE_PATH.read_text(encoding="utf-8").splitlines()[1:]]
    check("jiný seed → jiný vzorek (random vrstva)",
          {r["id"] for r in rows3} != set(ids) or len(ids) >= 41)

    # ── read-only ──
    check("audit nezměnil ani řádek v DB", db_fingerprint() == before)

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
