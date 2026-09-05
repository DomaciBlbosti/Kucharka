"""Pevný číselník kategorií surovin.

Proč vznikl. Kategorizace měla pevnou jen PRVNÍ úroveň (`categorize.TOP`),
druhou a třetí si model dopisoval volným textem. Výsledek byl seznam, který
se nedal projít očima: desítky kategorií, dvojice se stejným významem
(„ostatní > přísady" vs „ostatní > aditiva", „zelenina > kořenová zelenina"
vs „zelenina > kořeninoviny") a rovnou nesmysly z pokaženého překladu
(„maso > prasine", „ryby a mořské plody > sladkoviny", „sladidla > dezerty").

Číselník je proto UZAVŘENÝ a má právě dvě úrovně. Model si nevymýšlí text,
jen vybírá číslo z nabídky (viz categorize), takže nová varianta téhož už
vzniknout nemůže. Dvě úrovně schválně: filtr receptů stejně hlouběji nejde
(routers/ingredients.categories) a třetí úroveň byla přesně to místo, kde se
seznam trhal na kusy.

Top kategorie beze změny – jsou už uložené v `ingredient.category` a mění se
jen podúrovně. Top bez podkategorií (vejce) je v pořádku: cesta je pak jen
„vejce".
"""
from __future__ import annotations

from functools import lru_cache

from . import textnorm

# Top kategorie → povolené podkategorie. Prázdný seznam = top se nedělí.
TAXONOMY: dict[str, list[str]] = {
    "maso": [
        "drůbež", "vepřové", "hovězí", "telecí", "jehněčí a skopové",
        "králík a zvěřina", "uzeniny a šunka", "vnitřnosti",
    ],
    "ryby a mořské plody": [
        "mořské ryby", "sladkovodní ryby", "mořské plody", "ryby v konzervě",
    ],
    "mléčné výrobky": [
        "mléko a smetana", "sýry", "jogurty a zakysané výrobky", "tvaroh",
        "rostlinné alternativy",
    ],
    "vejce": [],
    "zelenina": [
        "kořenová zelenina", "listová zelenina", "košťálová zelenina",
        "plodová zelenina", "cibulová zelenina", "lusková zelenina",
        "brambory", "houby", "naložená zelenina",
    ],
    "ovoce": [
        "jádrové ovoce", "peckové ovoce", "bobulové ovoce", "citrusy",
        "exotické ovoce", "sušené ovoce", "kompoty",
    ],
    "obiloviny a pečivo": [
        "mouka", "rýže", "těstoviny", "chléb a pečivo", "vločky a müsli",
        "krupice a kroupy", "škroby", "sušenky a oplatky",
    ],
    "luštěniny": ["fazole", "čočka", "hrách", "cizrna", "sója a tofu"],
    "ořechy a semínka": ["ořechy", "semínka", "ořechová másla a pasty"],
    "tuky a oleje": ["rostlinné oleje", "máslo a sádlo", "margaríny"],
    "koření a bylinky": [
        "mleté koření", "celé koření", "čerstvé bylinky", "sušené bylinky",
        "kořenicí směsi", "sůl",
    ],
    "sladidla": ["cukr", "med a sirupy", "umělá sladidla", "čokoláda a kakao"],
    "nápoje": [
        "voda a minerálky", "džusy a šťávy", "káva", "čaj",
        "alkoholické nápoje", "sirupy a koncentráty",
    ],
    "ostatní": [
        "omáčky a dresinky", "ocet", "vývary a bujóny", "konzervy a hotová jídla",
        "přísady do pečení", "přídatné látky", "nepotravinové položky",
    ],
}

TOP: list[str] = list(TAXONOMY)

# Všechny platné cesty, v pořadí pro nabídku modelu.
PATHS: list[str] = [
    f"{top} > {sub}" if sub else top
    for top, subs in TAXONOMY.items()
    for sub in (subs or [""])
]

# Ruční převod známých variant na kanonickou cestu. Většina položek je
# z produkčního seznamu – buď synonymum („octy" → „ocet"), nebo pokažený
# překlad („prasine", „sladkoviny", „boby" z anglického beans).
#
# Klíče se píšou POŘÁDNOU ČEŠTINOU včetně diakritiky; na porovnávací tvar je
# převede `_key`. Psát je rovnou bez háčků nefunguje: stemmer se na text bez
# diakritiky chová jinak („saláty" → „salat", ale „salaty" → „sal"), takže by
# se takový alias nikdy netrefil. Hlídá to test.
_ALIASES: dict[str, str] = {
    # maso
    "prasine": "maso > vepřové",
    "prase": "maso > vepřové",
    "vepřové maso": "maso > vepřové",
    "drůbeží": "maso > drůbež",
    "kuře": "maso > drůbež",
    "kuřecí": "maso > drůbež",
    "hovězí maso": "maso > hovězí",
    "uzeniny": "maso > uzeniny a šunka",
    "salámy": "maso > uzeniny a šunka",
    "šunka": "maso > uzeniny a šunka",
    "zvěřina": "maso > králík a zvěřina",
    "králík": "maso > králík a zvěřina",
    "skopové": "maso > jehněčí a skopové",
    "jehněčí": "maso > jehněčí a skopové",
    # ryby
    "sladkoviny": "ryby a mořské plody > sladkovodní ryby",
    "sladkovodní": "ryby a mořské plody > sladkovodní ryby",
    "mořské": "ryby a mořské plody > mořské ryby",
    "ryby": "ryby a mořské plody > mořské ryby",
    "korýši": "ryby a mořské plody > mořské plody",
    "měkkýši": "ryby a mořské plody > mořské plody",
    "konzervované ryby": "ryby a mořské plody > ryby v konzervě",
    # mléčné
    "sýr": "mléčné výrobky > sýry",
    "mléko": "mléčné výrobky > mléko a smetana",
    "smetana": "mléčné výrobky > mléko a smetana",
    "jogurt": "mléčné výrobky > jogurty a zakysané výrobky",
    "zakysané": "mléčné výrobky > jogurty a zakysané výrobky",
    "rostlinné mléko": "mléčné výrobky > rostlinné alternativy",
    # zelenina
    "kořeninoviny": "zelenina > kořenová zelenina",
    "kořenová": "zelenina > kořenová zelenina",
    # POZOR: „kořeny" tu schválně NENÍ. Stemmer sráží „koření" i „kořeny" na
    # stejný tvar, takže by si ta dvě slova přebíjela klíč a jedno z nich by
    # se zařadilo špatně. „kořenová" tuhle kategorii pokrývá a „zelenina >
    # kořeny" radši propadne na model než do koření.
    "listová": "zelenina > listová zelenina",
    "saláty": "zelenina > listová zelenina",
    "salát": "zelenina > listová zelenina",
    "košťálová": "zelenina > košťálová zelenina",
    "zelí": "zelenina > košťálová zelenina",
    "plodová": "zelenina > plodová zelenina",
    "rajčata": "zelenina > plodová zelenina",
    "papriky": "zelenina > plodová zelenina",
    "cibulová": "zelenina > cibulová zelenina",
    "cibule": "zelenina > cibulová zelenina",
    "česnek": "zelenina > cibulová zelenina",
    "lusková": "zelenina > lusková zelenina",
    "brambor": "zelenina > brambory",
    "houby": "zelenina > houby",
    "naložená": "zelenina > naložená zelenina",
    "kvašená zelenina": "zelenina > naložená zelenina",
    # ovoce
    "jádrové": "ovoce > jádrové ovoce",
    "jablka": "ovoce > jádrové ovoce",
    "peckové": "ovoce > peckové ovoce",
    "bobuloviny": "ovoce > bobulové ovoce",
    "bobule": "ovoce > bobulové ovoce",
    "lesní plody": "ovoce > bobulové ovoce",
    "citrusy": "ovoce > citrusy",
    "exotické": "ovoce > exotické ovoce",
    "tropické ovoce": "ovoce > exotické ovoce",
    "sušené": "ovoce > sušené ovoce",
    "kompot": "ovoce > kompoty",
    # obiloviny
    "sušenky": "obiloviny a pečivo > sušenky a oplatky",
    "oplatky": "obiloviny a pečivo > sušenky a oplatky",
    "rýže": "obiloviny a pečivo > rýže",
    "těstoviny": "obiloviny a pečivo > těstoviny",
    "pečivo": "obiloviny a pečivo > chléb a pečivo",
    "chléb": "obiloviny a pečivo > chléb a pečivo",
    "vločky": "obiloviny a pečivo > vločky a müsli",
    "müsli": "obiloviny a pečivo > vločky a müsli",
    "obiloviny": "obiloviny a pečivo > krupice a kroupy",
    "škrob": "obiloviny a pečivo > škroby",
    # luštěniny
    "boby": "luštěniny > fazole",
    "fazol": "luštěniny > fazole",
    "sója": "luštěniny > sója a tofu",
    "tofu": "luštěniny > sója a tofu",
    # ořechy
    "kokos": "ořechy a semínka > ořechy",
    "ořech": "ořechy a semínka > ořechy",
    "semínka": "ořechy a semínka > semínka",
    "semena": "ořechy a semínka > semínka",
    # tuky
    "oleje": "tuky a oleje > rostlinné oleje",
    "olej": "tuky a oleje > rostlinné oleje",
    "máslo": "tuky a oleje > máslo a sádlo",
    "sádlo": "tuky a oleje > máslo a sádlo",
    "margarín": "tuky a oleje > margaríny",
    # koření
    "koření": "koření a bylinky > mleté koření",
    "bylinky": "koření a bylinky > čerstvé bylinky",
    "směsi": "koření a bylinky > kořenicí směsi",
    "sůl": "koření a bylinky > sůl",
    # sladidla
    "cukry": "sladidla > cukr",
    "med": "sladidla > med a sirupy",
    "sirupy": "sladidla > med a sirupy",
    "čokoláda": "sladidla > čokoláda a kakao",
    "kakao": "sladidla > čokoláda a kakao",
    # nápoje
    "ovocné nápoje": "nápoje > džusy a šťávy",
    "džusy": "nápoje > džusy a šťávy",
    "šťávy": "nápoje > džusy a šťávy",
    "alkohol": "nápoje > alkoholické nápoje",
    "víno": "nápoje > alkoholické nápoje",
    "pivo": "nápoje > alkoholické nápoje",
    "voda": "nápoje > voda a minerálky",
    "minerálky": "nápoje > voda a minerálky",
    # ostatní
    "octy": "ostatní > ocet",
    "omáčky": "ostatní > omáčky a dresinky",
    "dresinky": "ostatní > omáčky a dresinky",
    "vývary": "ostatní > vývary a bujóny",
    "bujón": "ostatní > vývary a bujóny",
    "konzervy": "ostatní > konzervy a hotová jídla",
    "aditiva": "ostatní > přídatné látky",
    "éčka": "ostatní > přídatné látky",
    "nepotravinové": "ostatní > nepotravinové položky",
}


def _key(s: str) -> str:
    """Klíč pro porovnávání názvů kategorií – bez diakritiky, přes stemmer.

    Stemmer je stejný jako u hledání, takže „sýr"/„sýry"/„sýra" splynou."""
    return " ".join(textnorm.tokens(s or ""))


_AMBIGUOUS = object()


@lru_cache(maxsize=1)
def _index() -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """Dva rejstříky: podle (top, název) a podle samotného názvu.

    Rejstřík s topem je potřeba, protože stemmer sráží různá slova na stejný
    tvar: „kořenová" (zelenina) i „koření" dají `koren`. Bez znalosti topu by
    si ty dvě kategorie klíč přebíjely a jedna by se zařazovala špatně.

    V rejstříku bez topu se kolize zahazují (`_AMBIGUOUS`) – když se dá klíč
    vyložit dvěma způsoby a top nepomůže, je správná odpověď „nevím".
    """
    by_top: dict[tuple[str, str], str] = {}
    by_key: dict[str, object] = {}

    def add(top: str, name: str, path: str) -> None:
        by_top.setdefault((_key(top), _key(name)), path)
        key = _key(name)
        if key in by_key and by_key[key] != path:
            by_key[key] = _AMBIGUOUS
        else:
            by_key.setdefault(key, path)

    for path in PATHS:
        parts = [p.strip() for p in path.split(">")]
        top = parts[0]
        add(top, path, path)
        add(top, parts[-1], path)
    for alias, path in _ALIASES.items():
        add(path.split(" > ")[0], alias, path)

    return by_top, {k: v for k, v in by_key.items() if v is not _AMBIGUOUS}


def is_valid(path: str) -> bool:
    return path in PATHS


def normalize_path(path: str | None) -> str | None:
    """Převeď libovolnou uloženou cestu na cestu z číselníku.

    Vrací `None`, když si nejsme jistí – takový záznam se radši nechá
    překategorizovat modelem, než aby se natvrdo zařadil špatně. Zařadit
    „sladidla > dezerty" odhadem by znamenalo vyrobit tichou chybu místo té
    hlučné, které si člověk všimne.
    """
    if not path:
        return None
    parts = [p.strip() for p in str(path).split(">") if p.strip()]
    if not parts:
        return None
    by_top, by_key = _index()

    top_key = _key(parts[0])
    top = next((t for t in TOP if _key(t) == top_key), None)

    # 1) v rámci známého topu – tam kolize stemů nevadí, „kořenová" pod
    #    zeleninou a „koření" pod kořením se nepletou
    if top is not None:
        for candidate in (*parts[1:], " > ".join(parts[:2]), " > ".join(parts)):
            hit = by_top.get((top_key, _key(candidate)))
            if hit:
                return hit

    # 2) celá cesta bez znalosti topu (top mohl být přejmenovaný)
    for candidate in (" > ".join(parts[:2]), " > ".join(parts)):
        hit = by_key.get(_key(candidate))
        if hit:
            return hit

    # 3) podle podúrovní – jen když sedí i top, jinak by „ostatní > sýr"
    #    přeskočilo do mléčných výrobků a tiše přepsalo zařazení
    for part in parts[1:]:
        hit = by_key.get(_key(part))
        if hit and (top is None or hit.split(" > ")[0] == top):
            return hit

    # 3) známý top bez použitelné podúrovně: nech aspoň top, když se nedělí;
    #    jinak to patří modelu
    if top is not None and not TAXONOMY[top]:
        return top
    return None
