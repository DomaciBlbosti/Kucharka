"""Testy čištění vyparsovaného receptu.

Regresní scénář z produkce: bestrecepty.cz dává do schema.org
recipeInstructions jako první řádek název receptu („Plněné papriky se šunkou
a sýrem 1. Papriky omyjeme…"). Ve vzorku 618 receptů to bylo u 16 z 26
receptů z této domény.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.modules.scraper import strip_title_prefix  # noqa: E402

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

    # Idempotence – migrace na pozadí se může spustit vícekrát.
    once = strip_title_prefix(f"{title} {STEPS}", title)
    check("druhý průchod už nic nemění",
          strip_title_prefix(once, title) == once, repr(once))

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
