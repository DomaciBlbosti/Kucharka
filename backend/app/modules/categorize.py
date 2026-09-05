"""Kategorizace surovin do hierarchie (např. 'maso > drůbeží > kuřecí').

Dávkově (víc surovin v jednom dotazu) a paralelně přes rychlý model.
Cesta se ukládá na surovinu (ingredient.category_path) – běží tedy jen jednou
pro nezkategorizované suroviny. Slouží k snadnějšímu hledání a filtrování.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, or_, select

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient
from . import llmclient, taxonomy

log = logging.getLogger("kucharka.categorize")

# Kategorie jsou UZAVŘENÝ číselník (viz modules/taxonomy). Dřív byla pevná
# jen tahle první úroveň a podúrovně si model dopisoval volným textem –
# vznikaly duplicity („přísady"/„aditiva") i nesmysly z pokaženého překladu
# („maso > prasine", „ryby > sladkoviny"). Teď model vybírá číslo z nabídky.
TOP = taxonomy.TOP

_BATCH = 25
_lock = threading.Lock()
_state: dict = {"running": False, "done": 0, "total": 0, "errors": 0, "finished_at": None}

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "c": {"type": "integer"},
                },
                "required": ["i", "c"],
            },
        }
    },
    "required": ["items"],
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def _inc(key: str, by: int = 1):
    with _lock:
        _state[key] = _state.get(key, 0) + by


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def status() -> dict:
    with _lock:
        s = dict(_state)
    db = SessionLocal()
    try:
        s["total_ingredients"] = db.scalar(select(func.count(Ingredient.id))) or 0
        s["uncategorized"] = db.scalar(
            select(func.count(Ingredient.id)).where(
                or_(Ingredient.category_path.is_(None), Ingredient.category_path == "")
            )
        ) or 0
    finally:
        db.close()
    s["last_error"] = llmclient.last_error()
    return s


def _categorize_batch(pairs: list[tuple[int, str]]) -> None:
    """pairs = [(id, name)]; přiřadí category_path a uloží."""
    if not llmclient.is_available() or not pairs:
        return
    listing = "\n".join(f"{i}. {name}" for i, (_id, name) in enumerate(pairs))
    # Nabídka kategorií jako číslovaný seznam. Model vrací ČÍSLO, ne text –
    # jinak si vymýšlí vlastní názvy a číselník se rozpadne.
    menu = "\n".join(f"{n}. {path}" for n, path in enumerate(taxonomy.PATHS))
    prompt = (
        "Zařaď každou potravinu do JEDNÉ kategorie ze seznamu níže. "
        "Odpověz číslem kategorie, nevymýšlej si vlastní názvy. "
        "Když si nejsi jistý, vyber nejbližší obecnější kategorii.\n"
        f"KATEGORIE:\n{menu}\n\n"
        f"POTRAVINY:\n{listing}\n\n"
        "Odpověz POUZE JSON {\"items\":[{\"i\":<číslo potraviny>,"
        "\"c\":<číslo kategorie>}]}."
    )
    out = llmclient.structured_json(
        prompt,
        schema=_SCHEMA,
        # stejný timeout jako dávkové párování – lokální model s plnou GPU
        # frontou 120s nestíhal a padaly VŠECHNY dávky
        timeout=max(settings.http_timeout, settings.llm_match_timeout_s),
        num_ctx=8192,
        component="kategorie",
    )
    if out is None:
        log.warning("kategorizace dávky selhala (volání modelu nebo parsování).")
        _inc("errors")
        _inc("done", len(pairs))
        return
    items = out.get("items", [])

    paths: dict[int, str] = {}
    for it in items:
        try:
            idx = int(it.get("i"))
            cat = int(it.get("c"))
        except Exception:  # noqa: BLE001 – model vrátil nečíslo
            continue
        # Mimo rozsah = model si vymyslel kategorii, která neexistuje.
        # Radši surovinu nechat nezařazenou, než ji zařadit náhodně.
        if 0 <= idx < len(pairs) and 0 <= cat < len(taxonomy.PATHS):
            paths[pairs[idx][0]] = taxonomy.PATHS[cat]

    db = SessionLocal()
    try:
        for ing_id, path in paths.items():
            ing = db.get(Ingredient, ing_id)
            if ing:
                ing.category_path = path
                if not ing.category:
                    ing.category = path.split(">")[0].strip()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("uložení kategorií selhalo: %s", exc)
        db.rollback()
    finally:
        db.close()
    _inc("done", len(pairs))


def categorize_all(only_missing: bool = True) -> None:
    _set(running=True, done=0, total=0, errors=0, finished_at=None)
    db = SessionLocal()
    try:
        stmt = select(Ingredient.id, Ingredient.name_cs)
        if only_missing:
            stmt = stmt.where(
                or_(Ingredient.category_path.is_(None), Ingredient.category_path == "")
            )
        rows = [(r[0], r[1]) for r in db.execute(stmt).all()]
    finally:
        db.close()
    _set(total=len(rows))
    batches = [rows[i : i + _BATCH] for i in range(0, len(rows), _BATCH)]
    workers = _effective_workers()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_categorize_batch, batches))
    finally:
        _set(running=False, finished_at=time.time())


def renormalize_all() -> dict:
    """Převeď už uložené kategorie na číselník (viz modules/taxonomy).

    Co se s uloženou cestou stane:
      * sedí na číselník (i po synonymech) → přepíše se na kanonický tvar,
      * nesedí a nedá se rozhodnout → cesta se VYMAŽE, takže surovinu při
        nejbližším běhu zařadí model, teď už z uzavřené nabídky.

    Mazat je schválně lepší než hádat: zařadit „sladidla > dezerty" odhadem
    znamená vyrobit tichou chybu místo hlučné, které si člověk všimne. Běží
    bez modelu, takže je to otázka vteřin.
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Ingredient.id, Ingredient.category_path)
            .where(Ingredient.category_path.isnot(None))
            .where(Ingredient.category_path != "")
        ).all()
        changed = cleared = kept = 0
        for ing_id, path in rows:
            target = taxonomy.normalize_path(path)
            if target == path:
                kept += 1
                continue
            ing = db.get(Ingredient, ing_id)
            if ing is None:
                continue
            if target is None:
                ing.category_path = None
                cleared += 1
            else:
                ing.category_path = target
                ing.category = target.split(">")[0].strip()
                changed += 1
        db.commit()
        log.info(
            "Kategorie srovnány s číselníkem: %s beze změny, %s přepsáno, "
            "%s vymazáno k překategorizování.", kept, changed, cleared,
        )
        return {"total": len(rows), "kept": kept, "changed": changed,
                "cleared": cleared}
    finally:
        db.close()


def _effective_workers() -> int:
    """Lokální Ollama zpracovává požadavky frontou – víc souběžných dávek si
    jen navzájem vyžírá timeout (8 workerů × pomalá GPU = padá všechno).
    Komerční API paralelismus zvládá, tam se bg_workers využije naplno."""
    workers = max(1, settings.bg_workers)
    if not settings.llm_api_enabled:
        workers = min(workers, 2)
    return workers


def categorize_async(only_missing: bool = True) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    threading.Thread(target=categorize_all, args=(only_missing,), daemon=True).start()
    return True
