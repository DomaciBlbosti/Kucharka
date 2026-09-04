"""Testy čištění vyparsovaného receptu a doménových pravidel.

Dva regresní scénáře z produkce, oba z bestrecepty.cz:
  1. do schema.org recipeInstructions jde jako první řádek název receptu
     („Plněné papriky se šunkou a sýrem 1. Papriky omyjeme…") – ve vzorku
     618 receptů u 16 z 26 receptů z této domény,
  2. u části receptů je v recipeInstructions JEDINÝ HowToStep, a to
     marketingový úvod („Nejlepší kuře na paprice je skvělý pokrm s…");
     kroky jsou jen v těle článku za nadpisem „Postup přípravy…".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.modules.scraper import strip_title_prefix  # noqa: E402
from app.modules.site_rules import has_rule, instructions_for  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


STEPS = ("Papriky omyjeme, zbavíme jader a očistíme. Sýry a šunku "
         "nastrouháme nahrubo a smícháme s vejcem.")


def main():
    title = "Plněné papriky se šunkou a sýrem"

    out = strip_title_prefix(f"{title} {STEPS}", title)
    check("název na začátku se odřízne", out == STEPS, repr(out))

    out = strip_title_prefix(f"{title}\n{STEPS}", title)
    check("odřízne se i s odřádkováním", out == STEPS, repr(out))

    out = strip_title_prefix(f"{title}: {STEPS}", title)
    check("odřízne se i s dvojtečkou", out == STEPS, repr(out))

    out = strip_title_prefix(f"{title.upper()} {STEPS}", title)
    check("nezáleží na velikosti písmen", out == STEPS, repr(out))

    # Název s upřesněním v závorce – v postupu se opakuje jen jeho začátek.
    long_title = "Rychlá domácí tatarka (tatarská omáčka)"
    out = strip_title_prefix(f"Rychlá domácí tatarka {STEPS}", long_title)
    check("zkrácená varianta názvu (závorka)", out == STEPS, repr(out))

    out = strip_title_prefix(f"Cukrová dekorace {STEPS}", "Cukrová dekorace - vazba růží")
    check("zkrácená varianta názvu (pomlčka)", out == STEPS, repr(out))

    # Co se odříznout NESMÍ
    check("postup bez názvu zůstane beze změny",
          strip_title_prefix(STEPS, title) == STEPS)
    check("podobný, ale jiný začátek zůstane",
          strip_title_prefix("Plněné papriky po italsku " + STEPS, title)
          == "Plněné papriky po italsku " + STEPS)
    short = f"{title} hotovo."
    check("krátký zbytek se neořezává (nevznikne prázdný postup)",
          strip_title_prefix(short, title) == short, repr(strip_title_prefix(short, title)))
    check("postup rovný názvu zůstane", strip_title_prefix(title, title) == title)
    check("krátký název se neřeší (falešné shody)",
          strip_title_prefix("Dort je hotový za hodinu a chutná výborně všem", "Dort")
          == "Dort je hotový za hodinu a chutná výborně všem")
    check("prázdný postup projde", strip_title_prefix("", title) == "")
    check("None projde", strip_title_prefix(None, title) is None)
    check("chybějící název projde", strip_title_prefix(STEPS, None) == STEPS)

    # Název jako PODMĚT první věty se ořezávat nesmí – jinak z toho zbude
    # troska „je lák, který…". Poznáme to podle malého písmene za názvem.
    subject = ("Sladkokyselý nálev na babiččiny okurky je lák, který je kompletně "
               "připravený bez dochucovadel a připravíte si jej z bylinek.")
    check("název jako podmět věty se neořízne",
          strip_title_prefix(subject, "Sladkokyselý nálev na babiččiny okurky")
          == subject, repr(strip_title_prefix(subject, "Sladkokyselý nálev na babiččiny okurky")))
    check("po názvu smí následovat číslo kroku",
          strip_title_prefix(f"{title} 1. {STEPS}", title) == f"1. {STEPS}")

    # Idempotence – migrace na pozadí se může spustit vícekrát.
    once = strip_title_prefix(f"{title} {STEPS}", title)
    check("druhý průchod už nic nemění",
          strip_title_prefix(once, title) == once, repr(once))

    # ── doménové pravidlo: kroky z těla článku ──
    check("bestrecepty.cz má pravidlo", has_rule("bestrecepty.cz"))
    check("i s www.", has_rule("www.bestrecepty.cz"))
    check("jiná doména pravidlo nemá", not has_rule("toprecepty.cz"))

    page = """
    <h2>Nejlepší kuře na paprice</h2>
    <p>Nejlepší kuře na paprice je skvělý pokrm, který vás zaručeně dostane.</p>
    <h3>Suroviny</h3>
    <ul><li>600 g kuřecích prsních řízků</li><li>3 červené papriky</li></ul>
    <h3>Postup p&#345;&#237;pravy ku&#345;ete na paprice</h3>
    <ol>
      <li>Ku&#345;ec&#237; prsa o&#269;ist&#283;te, osu&#353;te a nakr&#225;jejte na kostky.</li>
      <li>Do hrnce p&#345;idejte cibuli a restujte ji, dokud nezesklovat&#237;.</li>
      <li>Zelinu rozmixujte pono&#345;n&#253;m mix&#233;rem a zjemn&#283;te smetanou.</li>
    </ol>
    <h3>Chutnalo v&#225;m? Vyzkou&#353;ejte tak&#233;:</h3>
    <ul><li><a href="/x">Pe&#269;en&#233; ku&#345;e od babi&#269;ky</a></li></ul>
    """
    out = instructions_for("bestrecepty.cz", page)
    steps = (out or "").splitlines()
    check("pravidlo vytáhne kroky z <ol> za nadpisem Postup", len(steps) == 3, repr(out))
    check("entity jsou rozkódované",
          steps and steps[0].startswith("Kuřecí prsa očistěte"), repr(steps[:1]))
    check("úvod z <p> nad nadpisem se nebere",
          "skvělý pokrm" not in (out or ""), repr(out))
    check("suroviny nad nadpisem se neberou", "600 g" not in (out or ""))
    check("odkazy na jiné recepty se neberou",
          "babičky" not in (out or ""), repr(out))

    # Web střídá <ol> a sérii <div class="su-list"><ul><li>ikona + text</li>
    su = """
    <h3>Postup přípravy</h3>
    <div class="su-list"><ul><li><img src="o1.png" alt="" />
      Papriky omyjeme, zbavíme jader a očistíme.</li></ul></div>
    <div class="su-list"><ul><li><img src="o2.png" alt="" />
      Sýry a šunku nastrouháme nahrubo a promícháme s vejcem.</li></ul></div>
    <h3>Chutnaly vám?</h3>
    """
    out = instructions_for("bestrecepty.cz", su)
    check("pravidlo zvládne i su-list značkování",
          out and len(out.splitlines()) == 2, repr(out))
    check("obrázek v kroku nezanechá smetí",
          out and not out.startswith(" ") and "<" not in out, repr(out))

    # Alternativní nadpis, který web taky používá
    alt = ("<h3>Jak připravit nejlepší nakládané okurky?</h3><ol>"
           "<li>Na nálev smíchejte veškeré suroviny a provařte je 10 minut.</li>"
           "<li>Okurky zbavte nečistot a vyvařte si sklenice, do kterých je dáte.</li>"
           "</ol><h3>Povedlo se? Vyzkoušejte také:</h3>")
    check("pravidlo bere i nadpis 'Jak připravit…'",
          (instructions_for("bestrecepty.cz", alt) or "").count("\n") == 1,
          repr(instructions_for("bestrecepty.cz", alt)))

    # Když kroky nejsou, pravidlo musí couvnout na generický parser
    check("bez nadpisu Postup vrátí None",
          instructions_for("bestrecepty.cz",
                           "<h3>Suroviny</h3><ul><li>mouka</li></ul>") is None)
    check("jediný krok je málo (nejspíš špatný záchyt)",
          instructions_for("bestrecepty.cz",
                           "<h3>Postup</h3><ol><li>Vše smícháme a podáváme se salátem.</li></ol>")
          is None)
    check("krátké položky se neberou jako kroky",
          instructions_for("bestrecepty.cz",
                           "<h3>Postup</h3><ol><li>Vaříme</li><li>Podáváme</li></ol>") is None)
    check("doména bez pravidla vrací None", instructions_for("toprecepty.cz", page) is None)
    check("rozbité HTML nespadne", instructions_for("bestrecepty.cz", "<h3>Postup") is None)

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
