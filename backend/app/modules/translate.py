"""Překlad zahraničních receptů do češtiny přes LLM (Ollama, nebo komerční API).

České recepty (doména .cz nebo text s českou diakritikou) se nepřekládají.
U cizích se přeloží titul, ingredience a postup jedním dotazem; pokud se
nezachová počet ingrediencí, ponecháme originál (kvůli párování surovin).
Volání jde přes llmclient – při zapnutém komerčním API tedy překládá API
(výrazně lepší čeština), jinak lokální rychlý model.

Kromě překladu při importu umí modul i ZPĚTNĚ přeložit už uložené recepty
(retranslate_*), což využívá údržba v administraci — typicky pro recepty
stažené v době, kdy Ollama/model nebyl dostupný.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe

log = logging.getLogger("kucharka.translate")

_CZ_CHARS = set("ěščřžůňďť")

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "instructions": {"type": "string"},
    },
    "required": ["title", "ingredients", "instructions"],
}

# Malé lokální modely při překladu vymýšlejí novotvary („květička", „sóva
# semínka", „čirný pepřík"). Slovníček nejčastěji komolených termínů přímo
# v promptu je spolehlivě srovná; velkým API modelům nevadí.
_GLOSSARY = (
    "cauliflower=květák, florets=růžičky, broccoli=brokolice, "
    "sesame seeds=sezamová semínka, black pepper=černý pepř, "
    "garlic granules=granulovaný česnek, chickpeas=cizrna, "
    "baking tray/sheet=plech, parchment/baking paper=pečicí papír, "
    "tbsp=lžíce, tsp=lžička, cup=hrnek, zest=kůra, stock/broth=vývar, "
    "heavy/double cream=smetana ke šlehání, ground=mletý, simmer=mírně vařit"
)


def looks_czech(domain: str | None, text: str) -> bool:
    if domain and domain.endswith(".cz"):
        return True
    sample = (text or "")[:2000].lower()
    return any(c in _CZ_CHARS for c in sample)


def _translate_fields(title: str, ingredients: list[str], instructions: str) -> dict | None:
    """Zavolá LLM (přes llmclient) a vrátí přeložená pole, nebo None
    (nedostupný provider / chyba / nesedí počet ingrediencí)."""
    from . import llmclient

    if not llmclient.is_available():
        return None
    payload = {
        "title": title or "",
        "ingredients": list(ingredients),
        "instructions": instructions or "",
    }
    prompt = (
        "Jsi překladatel kuchařských receptů do češtiny. Přelož recept níže.\n"
        "Pravidla:\n"
        "- Používej výhradně zavedené české kuchyňské názvosloví; žádné "
        "novotvary ani doslovné kalky.\n"
        f"- Slovníček: {_GLOSSARY}.\n"
        "- Množství a čísla zachovej, jednotky přelož (tbsp→lžíce, tsp→lžička).\n"
        "- Zachovej PŘESNĚ počet a pořadí ingrediencí (řádek za řádek).\n"
        "- Postup piš přirozenou plynulou češtinou, vykej (smíchejte, pečte).\n"
        "Odpověz POUZE JSON objektem "
        '{"title": string, "ingredients": [string], "instructions": string}.\n'
        f"Recept: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        # llmclient drží globální Ollama zámek sám – překlad se na GPU
        # nepotká s párováním/kategorizací spuštěnými odjinud
        out = llmclient.structured_json(
            prompt,
            schema=_SCHEMA,
            timeout=max(settings.http_timeout, settings.llm_match_timeout_s),
            num_ctx=8192,
            # samostatný model jen pro překlad (experimenty s multilingválními
            # modely bez dopadu na párování); prázdné = rychlý model
            ollama_model=settings.translate_model or None,
        )
        if out is None:
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("překlad selhal: %s", exc)
        return None

    new_ing = out.get("ingredients")
    if not isinstance(new_ing, list) or len(new_ing) != len(ingredients):
        log.info("překlad zahozen (nesedí počet ingrediencí)")
        return None
    return {
        "title": (out.get("title") or title or "").strip(),
        "ingredients": [str(x) for x in new_ing],
        "instructions": out.get("instructions") or instructions or "",
    }


def translate_recipe(data: dict) -> dict:
    """Přelož recept (dict při importu) do češtiny, je-li cizí.

    Původní (nepřeložený) text se uloží do original_* klíčů, aby ho appka
    mohla později zobrazit / na něj přepnout – recept se v UI ukáže česky,
    s možností podívat se na předlohu.
    """
    from . import llmclient

    if not settings.translate_to_cs or not llmclient.is_available():
        return data
    probe = f"{data.get('title', '')} {data.get('instructions') or ''}"
    if looks_czech(data.get("source_domain"), probe):
        return data
    orig_title = data.get("title", "")
    orig_ingredients = list(data.get("ingredients", []))
    orig_instructions = data.get("instructions") or ""
    res = _translate_fields(orig_title, orig_ingredients, orig_instructions)
    if not res:
        return data
    data["original_title"] = orig_title
    data["original_ingredients"] = orig_ingredients
    data["original_instructions"] = orig_instructions or None
    data["title"] = res["title"]
    data["ingredients"] = res["ingredients"]
    if res["instructions"]:
        data["instructions"] = res["instructions"]
    data["translated_from"] = data.get("source_domain")
    return data


def is_foreign(recipe: Recipe) -> bool:
    return not looks_czech(
        recipe.source_domain, f"{recipe.title} {recipe.instructions or ''}"
    )


def retranslate_recipe(db, recipe: Recipe) -> bool:
    """Přelož už uložený recept (titul, postup, raw_text ingrediencí).

    Zachová originál – pokud už je uložený (např. z předchozího běhu), znovu
    ho nepřepisuje, ať se v případě opakovaného spuštění neztratí.
    """
    texts = [ri.raw_text for ri in recipe.ingredients]
    res = _translate_fields(recipe.title, texts, recipe.instructions or "")
    if not res:
        return False
    if recipe.original_title is None:
        recipe.original_title = recipe.title
        recipe.original_instructions = recipe.instructions
    recipe.title = res["title"]
    if res["instructions"]:
        recipe.instructions = res["instructions"]
    for ri, new in zip(recipe.ingredients, res["ingredients"]):
        if ri.original_raw_text is None:
            ri.original_raw_text = ri.raw_text
        ri.raw_text = new
    db.commit()
    return True


# ---- hromadný zpětný překlad (na pozadí, s progresem) ----

_lock = threading.Lock()
_state: dict = {"running": False, "done": 0, "total": 0, "translated": 0, "finished_at": None}


def _set(**kw):
    with _lock:
        _state.update(kw)


def _inc(key: str, by: int = 1):
    with _lock:
        _state[key] = _state.get(key, 0) + by


def is_running() -> bool:
    """Jen paměťový flag, žádný DB dotaz (na rozdíl od status())."""
    with _lock:
        return bool(_state["running"])


def status() -> dict:
    from . import llmclient

    with _lock:
        s = dict(_state)
    s["last_error"] = llmclient.last_error()
    db = SessionLocal()
    try:
        s["recipes_total"] = db.scalar(select(func.count(Recipe.id))) or 0
        s["foreign_estimate"] = db.scalar(
            select(func.count(Recipe.id)).where(
                (Recipe.source_domain.is_(None)) | (~Recipe.source_domain.like("%.cz"))
            )
        ) or 0
    finally:
        db.close()
    return s


def _retranslate_one(recipe_id: int) -> bool:
    db = SessionLocal()
    try:
        r = db.scalar(
            select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
        )
        if r is None or not is_foreign(r):
            return False
        return retranslate_recipe(db, r)
    except Exception as exc:  # noqa: BLE001
        log.warning("retranslate recipe %s selhal: %s", recipe_id, exc)
        db.rollback()
        return False
    finally:
        db.close()


def retranslate_all() -> None:
    _set(running=True, done=0, total=0, translated=0, finished_at=None)
    db = SessionLocal()
    try:
        ids = [r.id for r in db.scalars(select(Recipe)).all() if is_foreign(r)]
    finally:
        db.close()
    _set(total=len(ids))
    workers = max(1, settings.bg_workers)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_retranslate_one, ids):
                if ok:
                    _inc("translated")
                _inc("done")
    finally:
        _set(running=False, finished_at=time.time())


def retranslate_async() -> bool:
    # running se nastavuje atomicky hned tady pod zámkem, aby dvě souběžná
    # volání nemohla obě projít kontrolou dřív, než retranslate_all() (ve
    # vlákně) stihne running nastavit samo.
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    threading.Thread(target=retranslate_all, daemon=True).start()
    return True


# ---- hromadné znovupřeložení z ULOŽENÝCH originálů ----
#
# Pro recepty přeložené starým (mizerným) promptem: originál je uložený
# (original_title / original_raw_text), takže se nemusí nic stahovat z webu –
# jen se originál přeloží znovu, aktuální cestou (lepší prompt / jiný model /
# komerční API). Volitelný filtr na doménu, ať jde opravit třeba jen
# bbcgoodfood.com. Vazby surovin se nechávají – nenapárované řádky s novým
# textem si dorovná nejbližší kolečko zpracování.

_orig_lock = threading.Lock()
_orig_state: dict = {
    "running": False, "done": 0, "total": 0, "translated": 0,
    "domain": None, "finished_at": None,
}


def _orig_set(**kw):
    with _orig_lock:
        _orig_state.update(kw)


def _orig_inc(key: str, by: int = 1):
    with _orig_lock:
        _orig_state[key] = _orig_state.get(key, 0) + by


def originals_status() -> dict:
    with _orig_lock:
        s = dict(_orig_state)
    db = SessionLocal()
    try:
        s["candidates"] = db.scalar(
            select(func.count(Recipe.id)).where(Recipe.original_title.is_not(None))
        ) or 0
    finally:
        db.close()
    from . import llmclient

    s["last_error"] = llmclient.last_error()
    return s


def _retranslate_original_one(recipe_id: int) -> bool:
    """Přelož jeden recept znovu z uložených originálů. Vrací True při úspěchu."""
    db = SessionLocal()
    try:
        r = db.scalar(
            select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
        )
        if r is None or not r.original_title:
            return False
        texts = [ri.original_raw_text or ri.raw_text for ri in r.ingredients]
        res = _translate_fields(r.original_title, texts, r.original_instructions or "")
        if not res:
            return False
        r.title = res["title"]
        if res["instructions"]:
            r.instructions = res["instructions"]
        from .enrichment import _parse_amount_unit
        from .nutrition import grams_for, kcal_for, recompute_recipe_kcal

        for ri, new in zip(r.ingredients, res["ingredients"]):
            ri.raw_text = new[:400]
            ri.amount, ri.unit = _parse_amount_unit(ri.raw_text)
            if ri.ingredient_id:
                ri.grams = grams_for(ri.amount, ri.unit, ri.ingredient)
                ri.kcal = kcal_for(ri.grams, ri.ingredient)
        recompute_recipe_kcal(r)
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("znovupřeklad z originálu (recept %s) selhal: %s", recipe_id, exc)
        db.rollback()
        return False
    finally:
        db.close()


def retranslate_originals_all(domain: str | None = None) -> None:
    _orig_set(
        running=True, done=0, total=0, translated=0,
        domain=domain or None, finished_at=None,
    )
    db = SessionLocal()
    try:
        stmt = select(Recipe.id).where(Recipe.original_title.is_not(None))
        if domain:
            stmt = stmt.where(Recipe.source_domain == domain.strip().lower())
        ids = list(db.scalars(stmt).all())
    finally:
        db.close()
    _orig_set(total=len(ids))
    from .categorize import _effective_workers

    workers = _effective_workers()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_retranslate_original_one, ids):
                if ok:
                    _orig_inc("translated")
                _orig_inc("done")
    finally:
        _orig_set(running=False, finished_at=time.time())


def retranslate_originals_async(domain: str | None = None) -> bool:
    with _orig_lock:
        if _orig_state["running"]:
            return False
        _orig_state["running"] = True
    threading.Thread(
        target=retranslate_originals_all, args=(domain,), daemon=True
    ).start()
    return True


# ---- reset a znovupřeložení (stáhne čerstvý originál ze zdroje) ----
#
# Překlad přepisuje text přímo (žádná kopie originálu se neukládá), takže
# "smazat překlad" samo o sobě nedá nic zpátky. Jediná čistá cesta je znovu
# stáhnout originál ze zdrojové URL a přeložit ho znovu – to už umí ingest
# pipeline, jen ji tady voláme zpětně pro už uložené recepty.

_reset_lock = threading.Lock()
_reset_state: dict = {"running": False, "done": 0, "total": 0, "reset": 0, "finished_at": None}


def _reset_set(**kw):
    with _reset_lock:
        _reset_state.update(kw)


def _reset_inc(key: str, by: int = 1):
    with _reset_lock:
        _reset_state[key] = _reset_state.get(key, 0) + by


def needs_reset(recipe: Recipe) -> bool:
    """Cizí doména, text vypadá česky, ale originál není uložený = starý
    překlad z doby před ukládáním originálu. Nové překlady originál mají
    vždy (viz translate_recipe), takže se sem po chvíli přestanou trefovat."""
    if recipe.original_title:
        return False
    dom = recipe.source_domain
    if not dom or dom.endswith(".cz"):
        return False
    if not recipe.source_url or recipe.source_url.startswith(("photo://", "ai://")):
        return False  # není odkud stáhnout originál
    return looks_czech(None, f"{recipe.title} {recipe.instructions or ''}")


def reset_status() -> dict:
    with _reset_lock:
        s = dict(_reset_state)
    db = SessionLocal()
    try:
        s["candidates"] = sum(1 for r in db.scalars(select(Recipe)).all() if needs_reset(r))
    finally:
        db.close()
    return s


def _reset_one(recipe_id: int) -> bool:
    from . import ingest  # lazy import – translate <-> ingest by se jinak kruhově importovaly

    db = SessionLocal()
    try:
        r = db.get(Recipe, recipe_id)
        if r is None or not needs_reset(r):
            return False
        fresh = ingest.ingest_url(db, r.source_url)
        return fresh is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("reset překladu receptu %s selhal: %s", recipe_id, exc)
        return False
    finally:
        db.close()


def reset_translations_all() -> None:
    _reset_set(running=True, done=0, total=0, reset=0, finished_at=None)
    db = SessionLocal()
    try:
        ids = [r.id for r in db.scalars(select(Recipe)).all() if needs_reset(r)]
    finally:
        db.close()
    _reset_set(total=len(ids))
    workers = max(1, settings.bg_workers)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_reset_one, ids):
                if ok:
                    _reset_inc("reset")
                _reset_inc("done")
    finally:
        _reset_set(running=False, finished_at=time.time())


def reset_translations_async() -> bool:
    with _reset_lock:
        if _reset_state["running"]:
            return False
        _reset_state["running"] = True
    threading.Thread(target=reset_translations_all, daemon=True).start()
    return True
