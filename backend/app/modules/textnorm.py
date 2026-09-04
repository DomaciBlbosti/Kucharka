"""Normalizace českého textu pro hledání – lehký stemmer.

Proč ne lemmatizace: `lookup.py` používá simplemmu na klíče surovin a u
podstatných jmen funguje dobře, ale u sloves je nespolehlivá – „péct" vrátí
jako lemma „dopéct", „míchat" → „pomíchat", „pečeme" → „péct" ale „peču" →
„péci" (dvě různá lemmata pro jedno sloveso) a „nakrájíme" → „nakrájíst".
Pro hledání je to horší než nic: uživatel by u půlky tvarů nenašel nic.

Místo toho se používá lehký stemmer (varianta Dolamic & Savoy pro češtinu):
odřízne pádové a osobní koncovky a srovná palatalizaci (č/c, ž/h, š/s). Není
lingvisticky přesný, ale je KONZISTENTNÍ – a to je pro hledání to podstatné,
protože stejnou funkcí se normalizuje jak uložený text, tak dotaz.

Zvlášť se řeší kuchařská slovesa: „péct / peču / pečeme / upečeme / pekl"
stemmer sám nesrovná (mění se kmenová souhláska i samohláska). Pro ně je
tabulka kmenů převzatá z auditu korpusu (`corpus_audit.COOK_STEMS` a
`PREP_STEMS`) – když token začíná známým kmenem vařicího slovesa, použije se
rovnou ten kmen.

Diakritika se strhává až NA KONCI, pravidla stemmeru ji potřebují.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Slova, která v receptu nenesou informaci a jen nafukují index.
STOPWORDS = frozenset("""
a i o u v s k z na do od po za pro při pod nad mezi bez

je jsou byl byla bylo být budou bude
se si že jak aby nebo ale také už jen ještě pak potom nyní teď
tak takto tím tom ten ta to ty tyto této toho tomu
což který která které kterou kterým
my vy oni jsme jste
ne ano více méně velmi asi cca zhruba přibližně
podle dle podle
""".split())

_WORD_RE = re.compile(r"[0-9a-zà-žá-ž]+", re.I)


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


# ─── Lehký stemmer pro češtinu ───────────────────────────────────────────────

def _palatalise(w: str) -> str:
    """Srovnej palatalizaci na konci kmene (ruce→ruk, noze→noh, myši→myš)."""
    if w.endswith(("ci", "ce", "či", "če")):
        return w[:-2] + "k"
    if w.endswith(("zi", "ze", "ži", "že")):
        return w[:-2] + "h"
    if w.endswith(("čtě", "čti", "čtí")):
        return w[:-3] + "ck"
    if w.endswith(("ště", "šti", "ští")):
        return w[:-3] + "sk"
    return w[:-1]


def _remove_case(w: str) -> str:
    n = len(w)
    if n > 7 and w.endswith("atech"):
        return w[:-5]
    if n > 6:
        if w.endswith(("ětem", "atům")):
            return w[:-4] if w.endswith("atům") else _palatalise(w[:-3])
        if w.endswith(("ostí", "ovi", "ovy", "ova", "ové", "ovo")):
            return w[:-4] if w.endswith("ostí") else w[:-3]
    if n > 5:
        if w.endswith(("emi", "ete", "eti", "iho", "ího", "ěmi", "imu")):
            return _palatalise(w[:-2]) if w.endswith(("emi", "ete", "eti", "ěmi")) else w[:-3]
        if w.endswith(("ách", "ata", "aty", "ých", "ích", "ama", "ami",
                       "ové", "ovi", "ými", "ími")):
            return w[:-3]
    if n > 4:
        if w.endswith(("em", "es", "ém", "ím")):
            return _palatalise(w[:-1]) if w.endswith("em") else w[:-2]
        if w.endswith(("ům", "at", "ám", "os", "us", "ým", "mi", "ou")):
            return w[:-2]
    if n > 3:
        if w[-1] in "eiíě":
            return _palatalise(w)
        if w[-1] in "uyůaoáéý":
            return w[:-1]
    return w


def _remove_possessive(w: str) -> str:
    n = len(w)
    if n > 5 and w.endswith(("ov", "ův")):
        return w[:-2]
    if n > 6 and w.endswith("in"):
        return _palatalise(w[:-1])
    return w


# Vkladné („pohyblivé") e: mrkev/mrkve, hrnec/hrnce, hrášek/hrášku, pánev/pánve.
# Bez tohohle kroku se první pád rozchází se všemi ostatními.
_FLEETING_E_RE = re.compile(r"e([kcvň])$")


def _remove_fleeting_e(w: str) -> str:
    return _FLEETING_E_RE.sub(r"\1", w) if len(w) > 3 else w


# Nepravidelnosti, které lehký stemmer nesrovná – rozchází se u nich kmen,
# ne jen koncovka. Krátký ruční seznam vysokofrekvenčních kuchařských slov
# je poctivější než další obecné pravidlo, které by rozbilo jinde.
# Klíč i hodnota jsou BEZ diakritiky (porovnává se až po jejím odstranění).
_IRREGULAR = {
    # vejce / vajec / vajíčko
    "vejce": "vejc", "vejci": "vejc", "vejcem": "vejc", "vajec": "vejc",
    "vajicko": "vejc", "vajicka": "vejc", "vajicek": "vejc", "vajickem": "vejc",
    # rajče / rajčata
    "rajce": "rajc", "rajcete": "rajc",
    # hrnec / hrnce (palatalizace by udělala „hrnk")
    "hrnec": "hrnc", "hrnci": "hrnc", "hrnce": "hrnc", "hrncem": "hrnc",
    # sůl / soli / solí
    "sul": "sul", "soli": "sul",
    # chléb / chleba / chlebem
    "chleb": "chleb", "chleba": "chleb", "chlebu": "chleb", "chlebem": "chleb",
}


# ─── Kuchařská slovesa ───────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _verb_stems() -> tuple[str, ...]:
    """Kmeny vařicích sloves a úkonů z auditu korpusu, bez diakritiky.

    Import je líný a v try/except: `corpus_audit` tahá db/models a
    `textnorm` musí jít použít i samostatně (testy, skripty).

    Delší kmeny první, ať „nastrouh" vyhraje nad „strouh".
    """
    try:
        from .corpus_audit import COOK_STEMS, PREP_STEMS
        stems = set(COOK_STEMS) | set(PREP_STEMS)
    except Exception:  # noqa: BLE001
        return ()
    # „restu" je v auditu kvůli prefixovému hledání („restujeme"); tady se
    # koncovky odřezávají zvlášť, takže potřebujeme holý kmen.
    stems.discard("restu")
    stems.add("rest")
    return tuple(sorted(stems, key=len, reverse=True))


@lru_cache(maxsize=1)
def _stem_base() -> dict[str, str]:
    """Kmen → základní kmen bez vidové předpony („upec"→„pec", „osmaz"→„smaz").

    Pro hledání je sloučení žádoucí: kdo hledá „smažit", chce i „usmažíme".
    Předpona se odřezává jen tehdy, když zbytek je taky známý kmen – „obal"
    tedy zůstane „obal" a nerozpadne se na „bal"."""
    stems = set(_verb_stems())
    base: dict[str, str] = {}
    for stem in stems:
        cur = stem
        for _ in range(3):  # „rozpromích" se v korpusu nevyskytuje, 3 stačí
            for pref in _VERB_PREFIXES:
                rest = cur[len(pref):]
                if cur.startswith(pref) and rest in stems:
                    cur = rest
                    break
            else:
                break
        base[stem] = _STEM_ALIASES.get(cur, cur)
    return base


# Co smí za kmenem zbýt, aby to ještě byl tvar téhož slovesa. Bez tohohle
# filtru by „SPECiální" (s+pec) skončilo jako „pec" a „VARianta" jako „var" –
# přesně ty false positives, na které naráží i audit korpusu.
_VERB_TAILS = frozenset("""
 e em eme es ete te u i im ime is ite ame ate aji am as a
 l la lo li ly il ila ilo ili ily al ala alo ali aly el ela elo eli ely
 en ena eno eni eny an ana ano ani any
 t ct ci it at et ut ta ty
 uj uje ujem ujeme ujes ujete ujte uji ujou ujic
 ej ejte ejme ejic
 eneho enou enym enymi aneho anou anym anymi eny ena
 ouc ouci
 ovat oval ovala ovalo ovali ovaly ovan ovana ovano ovani ovany
 ava avam avame avas avate avaji avat aval avala avalo avali
 avej avejte avejme avan avana avano avani avany
""".split()) | {""}

# Předpony, které mění vid, ne význam akce – pro hledání je chceme sloučit
# („usmažíme" i „smažíme" má najít totéž).
_VERB_PREFIXES = ("nej", "ne", "roz", "pre", "pri", "pro", "pod", "nad", "ode",
                  "od", "vy", "za", "na", "po", "do", "ob", "u", "o", "s", "z", "v")

# Kmeny, které jsou jen hláskovou obměnou jiného (péct/pekl).
_STEM_ALIASES = {"pek": "pec", "upek": "upec"}


def _match_verb_stem(bare: str) -> str | None:
    for stem in _verb_stems():
        if bare.startswith(stem) and bare[len(stem):] in _VERB_TAILS:
            return _stem_base()[stem]
    return None


def _verb_stem(bare: str) -> str | None:
    """Kmen vařicího slovesa, pokud je token jeho tvarem (vstup bez diakritiky).

    Řeší to, co stemmer neumí: „péct / peču / pečeme / pekli / upečeme" mají
    různou kmenovou souhlásku i samohlásku, ale všechny začínají některým ze
    známých kmenů. Když token nesedí přímo, zkusí se ještě po odstranění
    vidové předpony, aby „usmažíme" splynulo se „smažíme".
    """
    direct = _match_verb_stem(bare)
    if direct is not None:
        return direct
    for pref in _VERB_PREFIXES:
        if bare.startswith(pref) and len(bare) - len(pref) >= 3:
            inner = _match_verb_stem(bare[len(pref):])
            if inner is not None:
                return inner
    return None


# ─── Veřejné API ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=100_000)
def stem_word(word: str) -> str:
    """Jedno slovo → normalizovaný tvar (lowercase, bez diakritiky).

    Pořadí: nepravidelnosti → vařicí slovesa → lehký stemmer. Odvozovací
    přípony se ZÁMĚRNĚ neodřezávají: „mouka" by se scvrkla na „mou" a
    kolidovala s „moučka" i „moucha".
    """
    w = word.lower()
    if w.isdigit():
        return w
    bare = strip_accents(w)
    if bare in _IRREGULAR:
        return _IRREGULAR[bare]
    verb = _verb_stem(bare)
    if verb is not None:
        return verb
    if len(w) <= 3:
        return bare
    w = _remove_fleeting_e(_remove_possessive(_remove_case(w)))
    return strip_accents(w)


def tokens(text: str) -> list[str]:
    """Text → seznam normalizovaných tokenů bez stopslov a bez duplicit
    vedle sebe. Krátké zbytky (1 znak) se zahazují – v indexu nic neřeší."""
    out: list[str] = []
    for raw in _WORD_RE.findall(text or ""):
        if raw.lower() in STOPWORDS:
            continue
        s = stem_word(raw)
        if len(s) >= 2 and (not out or out[-1] != s):
            out.append(s)
    return out


def normalize(text: str) -> str:
    """Text → normalizovaný text pro fulltext index i pro dotaz."""
    return " ".join(tokens(text))


def search_text_for(title: str | None, instructions: str | None,
                    ingredients: list[str] | None = None) -> str:
    """Normalizovaný text jednoho receptu.

    Suroviny jdou do indexu taky – hledání „hermelín" má najít i recept,
    který hermelín má v seznamu surovin, ale v postupu ho nejmenuje.
    Název se opakuje dvakrát, aby shoda v názvu vážila víc než v postupu.
    """
    parts = [title or "", title or "", instructions or ""]
    parts.extend(ingredients or [])
    return normalize(" ".join(parts))


def refresh_search_text(recipe) -> None:
    """Přepočítej `recipe.search_text` z aktuálních polí receptu.

    Volá se všude, kde se mění název, postup nebo suroviny – ingest,
    překlad, přeparsování postupů. Bez toho by index zůstal na starém textu.
    """
    recipe.search_text = search_text_for(
        recipe.title,
        recipe.instructions,
        [ri.raw_text or "" for ri in recipe.ingredients],
    )
