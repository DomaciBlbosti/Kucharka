"""Testy normalizace textu pro hledání (lehký český stemmer).

Cíl není lingvistická správnost, ale KONZISTENCE: všechny tvary jednoho
slova musí dát stejný kmen, protože stejnou funkcí se normalizuje uložený
text i dotaz. Kontroluje se proto po skupinách tvarů, ne jednotlivě.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-textnorm-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmpdir}/test.db")

from app.modules import textnorm  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


def same(label, *forms):
    stems = [textnorm.stem_word(w) for w in forms]
    check(f"{label}: {stems[0]}", len(set(stems)) == 1,
          " | ".join(f"{w}→{s}" for w, s in zip(forms, stems)))


def main():
    # ── slovesa: přesně případ ze zadání ──
    same("péct", "pečeme", "peču", "péct", "pečte", "pečený", "pečeno", "pekli")
    same("péct s předponou", "pečeme", "upečeme", "upéct", "zapečeme", "propéct")
    same("vařit", "vaříme", "vařím", "vařit", "vařte", "uvařte", "povaříme", "svařte")
    same("smažit", "smažíme", "smažit", "osmažte", "smažený", "usmažíme")
    same("krájet", "nakrájíme", "nakrájet", "nakrájejte", "krájíme", "rozkrájet")
    same("míchat", "míchejte", "míchat", "promícháme", "smícháme", "zamíchat")
    same("restovat", "restujeme", "orestujeme", "restovat", "orestovat")
    same("marinovat", "marinujeme", "marinovat", "marinovaná", "zamarinovat")
    same("přidat", "přidáme", "přidejte", "přidat", "přidávejte", "přidávat")
    same("podávat", "podáváme", "podávejte", "podávat")

    # ── suroviny a kuchyňské pojmy ──
    same("cibule", "cibule", "cibuli", "cibulí", "cibulemi")
    same("mouka", "mouka", "mouky", "mouku", "moukou", "mouce")
    same("vejce", "vejce", "vajec", "vejcem", "vajíčko")
    same("brambory", "brambory", "brambor", "bramborami", "brambora")
    same("mrkev", "mrkev", "mrkve", "mrkví")
    same("kuře", "kuře", "kuřete")
    same("kuřecí", "kuřecí", "kuřecího", "kuřecím")
    same("hermelín", "hermelín", "hermelínu", "hermelínem", "hermelíny")
    same("česnek", "česnek", "česneku", "česnekem")
    same("rajče", "rajče", "rajčata", "rajčaty", "rajčat")
    same("máslo", "máslo", "másla", "máslem", "másle")
    same("smetana", "smetana", "smetany", "smetanu", "smetanou")
    same("těsto", "těsto", "těsta", "těstem", "těstě")
    same("trouba", "troubu", "troubě", "trouba", "troubou")
    same("hrnec", "hrnec", "hrnci", "hrnce")
    same("pánev", "pánev", "pánvi", "pánve")
    same("paprika", "papriky", "paprika", "papriku", "paprikou")
    same("mléko", "mléko", "mléka", "mlékem", "mléce")
    same("hrášek", "hrášek", "hrášku", "hráškem")
    same("chléb", "chléb", "chleba", "chlebem", "chlebu")

    # ── co se NESMÍ splést s vařicím slovesem ──
    # („s"+"pec", „var"+ianta – přesně ty false positives, na které naráží
    # i audit korpusu; proto se za kmenem hlídá povolená koncovka)
    for word in ("speciální", "varianta", "varná", "pečivo", "mixér", "sekunda",
                 "pomazánka", "zavařenina", "surovina", "nádobí", "svačina"):
        s = textnorm.stem_word(word)
        check(f"'{word}' není sloveso ({s})",
              s not in {"pec", "var", "mix", "sek", "maz", "surov"} or s == "surov",
              s)

    # ── diakritika a velikost písmen ──
    check("diakritika nehraje roli",
          textnorm.stem_word("Máslo") == textnorm.stem_word("maslo"),
          f"{textnorm.stem_word('Máslo')} vs {textnorm.stem_word('maslo')}")
    check("výstup je bez diakritiky",
          textnorm.stem_word("kuřecí") == textnorm.strip_accents(textnorm.stem_word("kuřecí")))

    # ── tokenizace ──
    t = textnorm.tokens("Cibuli nakrájíme nadrobno a osmažíme na másle.")
    check("stopslova vypadnou", "na" not in t and "a" not in t, str(t))
    check("interpunkce vypadne", all(c.isalnum() for w in t for c in w), str(t))
    check("věta dá kmeny", t[:2] == ["cibul", "kraj"], str(t))
    check("prázdný vstup dá prázdno", textnorm.tokens("") == [] and textnorm.normalize(None) == "")
    check("čísla zůstávají", "180" in textnorm.tokens("pečeme při 180 °C"),
          str(textnorm.tokens("pečeme při 180 °C")))

    # ── text celého receptu ──
    st = textnorm.search_text_for(
        "Pečené kuře na paprice",
        "Kuře nakrájíme a pečeme v troubě 40 minut.",
        ["2 ks kuřecích prsou", "1 lžíce sladké papriky"],
    )
    for needle in ("kur", "pec", "paprik", "troub", "kraj"):
        check(f"search_text obsahuje '{needle}'", needle in st.split(), st)
    check("název je v textu dvakrát (váží víc)", st.split().count("kur") >= 2, st)

    # ── dotaz a text se potkají ──
    def finds(query, text_):
        q = [w for w in textnorm.tokens(query)]
        hay = set(textnorm.tokens(text_))
        return all(w in hay for w in q)

    check("'péct' najde 'pečeme'", finds("péct", "V troubě pečeme 40 minut."))
    check("'kuřecí prsa' najde 'kuřecích prsou'",
          finds("kuřecí prsa", "Použijeme 300 g kuřecích prsou."))
    check("'cibule' najde 'cibulí'", finds("cibule", "Zasypeme cibulí a česnekem."))
    check("bez diakritiky to taky najde", finds("cibule", "Zasypeme cibuli."))
    check("nesouvisející dotaz nenajde", not finds("čokoláda", "Zasypeme cibulí."))

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
