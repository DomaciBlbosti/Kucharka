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
# Kolik dávek se pošle na frontu jedním voláním (viz _run_batches).
_JOBS_CHUNK = 20
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


def _prompt_for(pairs: list[tuple[int, str]]) -> str:
    """Dotaz pro jednu dávku surovin. Vytažené zvlášť, ať se dá poskládat
    celá dávka dopředu a poslat na frontu proxy najednou."""
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
    return prompt


def _apply(pairs: list[tuple[int, str]], out: dict | None) -> None:
    """Zapiš odpověď modelu na jednu dávku surovin."""
    if out is None:
        log.warning("kategorizace dávky selhala (volání modelu nebo parsování).")
        _inc("errors")
        _inc("done", len(pairs))
        return

    paths: dict[int, str] = {}
    for it in out.get("items", []):
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


def _timeout() -> float:
    # Stejný timeout jako dávkové párování – lokální model s plnou GPU
    # frontou 120 s nestíhal a padaly VŠECHNY dávky.
    return max(settings.http_timeout, settings.llm_match_timeout_s)


def _categorize_batch(pairs: list[tuple[int, str]]) -> None:
    """Jedna dávka přímým voláním (bez fronty)."""
    if not llmclient.is_available() or not pairs:
        return
    _apply(pairs, llmclient.structured_json(
        _prompt_for(pairs), schema=_SCHEMA, timeout=_timeout(),
        num_ctx=8192, component="kategorie",
    ))


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
    try:
        _run_batches(batches)
    finally:
        _set(running=False, finished_at=time.time())


def _run_batches(batches: list[list[tuple[int, str]]]) -> None:
    """Zpracuj dávky – frontou proxy, když je zapnutá, jinak přímo.

    Přes frontu jde všech N dotazů jedním voláním, takže si je proxy může
    seskupit podle modelu a nahrát ho jednou. Posílat je po jednom by
    přednost interaktivních dotazů zařídilo taky, ale o seskupení bychom
    přišli – a přesně kvůli němu se to sem tahalo.

    Odesílá se po částech: 12 tisíc surovin naráz je 480 úloh v jednom
    dotazu, což je zbytečně velké sousto a při výpadku by se ztratilo všechno.
    """
    from . import llmjobs

    if not batches:
        return
    if not llmjobs.enabled():
        with ThreadPoolExecutor(max_workers=_effective_workers()) as ex:
            list(ex.map(_categorize_batch, batches))
        return

    for start in range(0, len(batches), _JOBS_CHUNK):
        chunk = batches[start:start + _JOBS_CHUNK]
        outs = llmclient.structured_json_many(
            [_prompt_for(b) for b in chunk], schema=_SCHEMA, timeout=_timeout(),
            num_ctx=8192, component="kategorie",
        )
        for pairs, out in zip(chunk, outs):
            _apply(pairs, out)


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
