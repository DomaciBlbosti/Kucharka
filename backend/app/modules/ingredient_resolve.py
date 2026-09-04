"""Jedno místo, kde se z názvu suroviny hledá KANONICKÝ záznam.

Proč to vzniklo: novou surovinu zakládaly dvě různé cesty a ani jedna
pořádně nekontrolovala, jestli už existuje.

  * `normalizer.create_ingredient_via_llm` nechal LLM vymyslet `name_cs` a
    ten rovnou zapsal – bez jakéhokoli lookupu. Řádek „3 lžíce olivového
    oleje" tak založil „olivový olej" i ve chvíli, kdy „olivový olej" v
    tabulce dávno byl (fuzzy match předtím běžel na surový text
    „olivového oleje", ne na výsledný název).
  * `llm_match.get_or_create_ingredient` porovnával jen přesnou shodu
    `lower(name_cs)`, takže „kuřecí prso" a „kuřecí prsa" jsou dvě suroviny.

Navíc `ingredient.name_cs` nemá UNIQUE, takže tomu nic nebránilo.

Hledá se ve třech vrstvách, od nejpřísnější:
  1. přesná shoda názvu (case-insensitive),
  2. shoda normalizovaného klíče – stemmer z `textnorm` + seřazená slova,
     takže „paprika mletá sladká" == „mletá sladká paprika",
  3. fuzzy (rapidfuzz, token_set_ratio) s vysokým prahem.

Při shodě víc záznamů vyhrává REFERENČNÍ z NutriDatabáze: ta je ručně
udržovaný katalog s reálnou výživou, kdežto `source='ollama'` je odhad.
Ukotvení na ni je způsob, jak slovník přestat plevelit.
"""
from __future__ import annotations

import logging
import threading

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Ingredient
from .textnorm import stem_word

log = logging.getLogger("kucharka.ingredients")

# Hodnota `ingredient.source` u záznamů z NutriDatabáze (viz seed/import_nutridb).
NUTRIDB_SOURCE = "nutridatabaze"

# Práh fuzzy shody. Vysoko schválně: radši surovinu založit dvakrát, než
# spojit „smetana ke šlehání" se „zakysanou smetanou".
_FUZZY_CUTOFF = 90


def name_key(name: str) -> str:
    """Normalizovaný klíč názvu, NEZÁVISLÝ na pořadí slov.

    „paprika mletá sladká" i „mletá sladká paprika" → „mlet paprik sladk".
    """
    words = sorted({stem_word(w) for w in (name or "").split() if len(w) > 1})
    return " ".join(w for w in words if w)


# ─── Cache mapy název → id ───────────────────────────────────────────────────
# Slovník má jednotky tisíc položek, ale `match_ingredient` ho dosud tahal z
# DB při KAŽDÉM řádku. Tady se drží v paměti a invaliduje se podle počtu
# řádků (nové suroviny přibývají, mizí prakticky nikdy).

_lock = threading.Lock()
_cache: dict = {"count": -1, "exact": {}, "keys": {}, "names": {}, "nutri": set()}


def _index(db: Session) -> dict:
    count = db.scalar(select(func.count(Ingredient.id))) or 0
    with _lock:
        if _cache["count"] == count:
            return _cache
    rows = db.execute(
        select(Ingredient.id, Ingredient.name_cs, Ingredient.source)
    ).all()
    exact: dict[str, list[int]] = {}
    keys: dict[str, list[int]] = {}
    names: dict[int, str] = {}
    nutri: set[int] = set()
    for iid, name, source in rows:
        if not name:
            continue
        names[iid] = name
        exact.setdefault(name.strip().lower(), []).append(iid)
        keys.setdefault(name_key(name), []).append(iid)
        if source == NUTRIDB_SOURCE:
            nutri.add(iid)
    with _lock:
        _cache.update(count=count, exact=exact, keys=keys, names=names, nutri=nutri)
        return _cache


def invalidate() -> None:
    """Zapomeň cache (volá se po založení suroviny)."""
    with _lock:
        _cache["count"] = -1


def _prefer(ids: list[int], nutri: set[int]) -> int | None:
    """Z několika shod vyber referenční z NutriDatabáze, jinak nejnižší id
    (= nejstarší záznam, na kterém nejspíš visí nejvíc receptů)."""
    if not ids:
        return None
    from_nutri = [i for i in ids if i in nutri]
    return min(from_nutri or ids)


def find_by_name(db: Session, name: str) -> Ingredient | None:
    """Najdi existující surovinu odpovídající názvu, nebo None."""
    clean = (name or "").strip()
    if len(clean) < 2:
        return None
    idx = _index(db)

    hit = _prefer(idx["exact"].get(clean.lower(), []), idx["nutri"])
    if hit is None:
        hit = _prefer(idx["keys"].get(name_key(clean), []), idx["nutri"])
    if hit is None:
        best = process.extractOne(
            name_key(clean), idx["keys"].keys(),
            scorer=fuzz.token_set_ratio, score_cutoff=_FUZZY_CUTOFF,
        )
        if best:
            hit = _prefer(idx["keys"][best[0]], idx["nutri"])
    return db.get(Ingredient, hit) if hit is not None else None


def get_or_create(db: Session, name: str, *, source: str = "ollama") -> Ingredient:
    """Najdi surovinu podle názvu, nebo ji založ.

    Zakládá se až po všech třech vrstvách hledání – tohle je jediné místo,
    přes které mají nové suroviny vznikat."""
    ing = find_by_name(db, name)
    if ing is not None:
        return ing
    ing = Ingredient(name_cs=name.strip(), source=source)
    db.add(ing)
    db.flush()
    invalidate()
    log.info("nová surovina %r (source=%s, id=%s)", ing.name_cs, source, ing.id)
    return ing
