"""Testy čitelného exportu receptů (HTML + XML) ke kontrole zpracování.

Export má ukázat všechny tři vrstvy textu vedle sebe – syrová data ze
scraperu, text před strojovým překladem a to, co uživatel vidí v appce – plus
výsledek párování surovin, metriky a tagy. Testy hlídají hlavně to, že se
žádná vrstva cestou neztratí, že je HTML i XML dobře uvozené (v názvech
receptů se běžně vyskytuje `&` a uvozovky) a že export nesahá na databázi.
"""
from __future__ import annotations

import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-export-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Ingredient, Recipe, RecipeIngredient, Tag,
)
from app.modules import recipe_export  # noqa: E402

Base.metadata.create_all(engine)

# výstupy do dočasného adresáře, ne do repa
recipe_export.ANALYSIS_DIR = Path(_tmpdir) / "analysis"
recipe_export.HTML_PATH = recipe_export.ANALYSIS_DIR / "recipe_export.html"
recipe_export.XML_PATH = recipe_export.ANALYSIS_DIR / "recipe_export.xml"

PASSED = FAILED = 0

# Název, který rozbije špatně uvozené HTML i XML.
NASTY_TITLE = 'Kuře & "smetana" <script>alert(1)</script>'


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
    mouka = Ingredient(name_cs="mouka hladká", kcal_100g=350,
                       category_path="pekárna > mouky")
    db.add(mouka)
    tag = Tag(namespace="chod", slug="hlavni", label_cs="Hlavní jídlo")
    db.add(tag)
    db.flush()

    # 1) přeložený recept – má obě verze textu
    prelozeny = Recipe(
        title="Palačinky s mákem", original_title="Pancakes with poppy seed",
        source_url="https://web.cz/1", source_domain="web.cz",
        instructions=(
            "Mouku smícháme s mlékem a vejci, dokud nevznikne hladké těsto. "
            "Necháme 30 minut odpočinout v lednici. Smažíme na pánvi 10 minut "
            "z každé strany dozlatova a podáváme s mákem a cukrem."
        ),
        original_instructions=(
            "Mix the flour with milk and eggs until smooth. Rest for 30 "
            "minutes. Fry for 10 minutes on each side and serve."
        ),
        raw_json='{"title": "Pancakes with poppy seed"}',
        rating=4.5, rating_count=20, feed_score=4.2,
    )
    prelozeny.tags.append(tag)
    db.add(prelozeny)
    db.flush()
    db.add(RecipeIngredient(recipe_id=prelozeny.id, raw_text="200 g mouky",
                            original_raw_text="200 g flour",
                            ingredient_id=mouka.id, amount=200, unit="g",
                            grams=200, kcal=700))

    # 2) nepřeložený, s NENAPÁROVANOU surovinou a s ne-surovinou
    nenaparovany = Recipe(
        title=NASTY_TITLE, source_url="https://web.cz/2", source_domain="web.cz",
        instructions=(
            "Kuře nakrájíme na kousky a opečeme na pánvi dozlatova. Zalijeme "
            "smetanou, osolíme a dusíme 20 minut pod pokličkou. Nakonec "
            "zahustíme moukou a necháme přejít varem."
        ),
    )
    db.add(nenaparovany)
    db.flush()
    db.add(RecipeIngredient(recipe_id=nenaparovany.id, raw_text="1 kuře"))
    db.add(RecipeIngredient(recipe_id=nenaparovany.id, raw_text="alobal",
                            nonfood=True))
    db.add(RecipeIngredient(recipe_id=nenaparovany.id, raw_text="200 g mouky",
                            ingredient_id=mouka.id, optional=True))

    # 3) prázdný postup – tenhle má výběr `no_instr` najít
    prazdny = Recipe(title="Bez postupu", source_url="https://jiny.cz/3",
                     source_domain="jiny.cz", instructions="Smíchat.")
    db.add(prazdny)
    db.flush()
    db.add(RecipeIngredient(recipe_id=prazdny.id, raw_text="cukr"))

    db.commit()
    ids = {"prelozeny": prelozeny.id, "nenaparovany": nenaparovany.id,
           "prazdny": prazdny.id}
    db.close()
    return ids


def db_fingerprint():
    db = SessionLocal()
    try:
        return (
            [(r.id, r.title, r.instructions, r.feed_score)
             for r in db.query(Recipe).order_by(Recipe.id)],
            [(r.id, r.raw_text, r.ingredient_id, r.grams)
             for r in db.query(RecipeIngredient).order_by(RecipeIngredient.id)],
        )
    finally:
        db.close()


def main():
    ids = seed_db()
    before = db_fingerprint()

    # ── základní běh ──
    print("\nexport:")
    out = recipe_export.run(limit=50, pick="random")
    check("export doběhne a vrátí počet", out["count"] == 3, str(out))
    check("vznikne HTML", recipe_export.HTML_PATH.exists())
    check("vznikne XML", recipe_export.XML_PATH.exists())

    doc = recipe_export.HTML_PATH.read_text(encoding="utf-8")

    # ── obě verze textu ──
    print("\nobě verze textu:")
    check("zobrazený postup je v HTML", "Smažíme na pánvi" in doc)
    check("originál před překladem je taky v HTML", "Fry for 10 minutes" in doc)
    check("zobrazený název je v HTML", "Palačinky s mákem" in doc)
    check("původní název je v HTML", "Pancakes with poppy seed" in doc)
    check("syrová data ze scraperu jsou v HTML", "raw_json" in doc)
    check("u nepřeloženého se needituje sloupec s originálem",
          "nepřekládáno" in doc)

    # ── párování surovin ──
    print("\npárování surovin:")
    check("řádek suroviny, jak ho vidí uživatel", "200 g mouky" in doc)
    check("originál řádku před překladem", "200 g flour" in doc)
    check("na co se to napárovalo", "mouka hladká" in doc)
    check("kategorie napárované suroviny", "pekárna &gt; mouky" in doc)
    check("nenapárovaná surovina je vidět", "nenapárováno" in doc)
    check("ne-surovina se odliší od nenapárované", "ne-surovina" in doc)
    check("volitelná surovina je označená", "(volitelné)" in doc)

    # ── čísla ──
    # Regrese: ořezávání koncových nul bralo nuly i celým číslům, takže se
    # z 200 g stalo „2" a ze 700 kcal „7". V tabulce surovin to nebylo poznat
    # jinak než okem, žádná jiná kontrola na to nesáhla.
    print("\nčísla:")
    check("celé gramy se neořežou", ">200<" in doc.replace('class="num">', '>'),
          "200 g se vypsalo jinak než 200")
    check("celé kcal se neořežou", ">700<" in doc.replace('class="num">', '>'))
    check("desetinné číslo si nuly navíc ořeže",
          recipe_export._num(4.20, 2) == "4.2", recipe_export._num(4.20, 2))
    check("celé číslo zůstane celé", recipe_export._num(200.0, 0) == "200",
          recipe_export._num(200.0, 0))
    check("prázdná hodnota je pomlčka", recipe_export._num(None) == "–")

    # České skloňování po číslovce – export se čte očima, ne parserem.
    check("1 surovina", recipe_export._plural(1, "surovina", "suroviny", "surovin")
          == "1 surovina")
    check("3 suroviny", recipe_export._plural(3, "surovina", "suroviny", "surovin")
          == "3 suroviny")
    check("5 surovin", recipe_export._plural(5, "surovina", "suroviny", "surovin")
          == "5 surovin")

    # ── metriky a tagy ──
    print("\nmetriky a tagy:")
    check("pokrytí surovin je v HTML", "pokrytí surovin" in doc)
    check("počet vařicích sloves", "vařicích sloves" in doc)
    check("skóre pro úvodní stránku", "skóre" in doc)
    check("tag i s namespace", "chod: Hlavní jídlo" in doc)

    # ── uvozování ──
    print("\nuvozování:")
    check("nebezpečný název se neproleje do HTML jako značka",
          "<script>alert(1)</script>" not in doc)
    check("ale text sám v exportu je", "alert(1)" in doc)

    xml_text = recipe_export.XML_PATH.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(xml_text)
        parsed = True
    except ET.ParseError as exc:
        root, parsed = None, False
        check("XML je dobře uvozené", False, str(exc))
    if parsed:
        check("XML je dobře uvozené", True)
        recs = root.findall("recipe")
        check("XML má všechny recepty", len(recs) == 3, str(len(recs)))
        by_id = {int(r.get("id")): r for r in recs}
        p = by_id[ids["prelozeny"]]
        check("XML nese zobrazený i původní postup",
              "Smažíme" in p.findtext("instructions")
              and "Fry for" in p.findtext("original_instructions"))
        check("XML označí přeložený recept", p.get("translated") == "true")
        check("XML nese metriky",
              p.find("metrics/ingr_coverage") is not None)
        check("XML nese tagy", p.findtext("tags/tag") == "Hlavní jídlo")
        n = by_id[ids["nenaparovany"]]
        unm = [i for i in n.findall("ingredients/ingredient")
               if i.get("unmatched") == "true"]
        check("XML označí nenapárovanou surovinu", len(unm) == 1, str(len(unm)))
        check("ne-surovina se nepočítá jako nenapárovaná",
              len(n.findall("ingredients/ingredient")) == 3)
        check("nebezpečný název přežije kolečko přes XML beze změny",
              by_id[ids["nenaparovany"]].findtext("title") == NASTY_TITLE)

    # ── výběrové režimy ──
    print("\nvýběr receptů:")
    out = recipe_export.run(limit=50, pick="translated")
    check("'translated' vybere jen přeložené", out["count"] == 1, str(out["count"]))
    out = recipe_export.run(limit=50, pick="unmatched")
    check("'unmatched' vybere jen ty s nenapárovanou surovinou",
          out["count"] == 2, str(out["count"]))
    out = recipe_export.run(limit=50, pick="no_instr")
    check("'no_instr' vybere jen ty s krátkým postupem",
          out["count"] == 1, str(out["count"]))
    out = recipe_export.run(limit=50, pick="random", domain="jiny.cz")
    check("filtr na doménu funguje", out["count"] == 1, str(out["count"]))
    out = recipe_export.run(limit=2, pick="random")
    check("limit se dodrží", out["count"] == 2, str(out["count"]))

    a = recipe_export.run(limit=2, pick="random", seed=7)
    txt_a = recipe_export.XML_PATH.read_text(encoding="utf-8")
    recipe_export.run(limit=2, pick="random", seed=7)
    txt_b = recipe_export.XML_PATH.read_text(encoding="utf-8")
    check("stejný seed → stejný výběr receptů",
          {r.get("id") for r in ET.fromstring(txt_a).findall("recipe")}
          == {r.get("id") for r in ET.fromstring(txt_b).findall("recipe")})

    try:
        recipe_export.run(pick="neexistuje")
        check("neznámý výběr skončí chybou", False, "nevyhodilo to nic")
    except ValueError:
        check("neznámý výběr skončí chybou", True)

    # ── řazení: nejhorší nahoru ──
    print("\nřazení:")
    recipe_export.run(limit=50, pick="random")
    root = ET.fromstring(recipe_export.XML_PATH.read_text(encoding="utf-8"))
    covs = [float(r.findtext("metrics/ingr_coverage"))
            for r in root.findall("recipe")]
    check("nejhůř pokryté recepty jsou první", covs == sorted(covs), str(covs))

    # ── read-only ──
    print("\nread-only:")
    check("export nezměnil ani řádek v DB", db_fingerprint() == before)

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
