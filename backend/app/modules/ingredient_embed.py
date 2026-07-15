"""Embeddingy surovin – pro dynamický výběr kandidátů do LLM matching promptu.

Stejný princip jako rag.py u receptů (settings.embed_model, float32 bytes
v DB, brute-force kosinus v numpy – pro tisíce surovin je to okamžité).

Proč: statický top-N katalog v llm_match.py (`_build_ingredient_catalog`)
bere jen nejpoužívanější suroviny. Vzácná/neobvyklá surovina, která v top-N
není, se pro LLM nikdy nemůže trefit – prostě není v nabídce (ověřeno na
produkčním běhu: pesto, tuzemák, sacharín, apetito... 0/40 napárováno,
přestože je model správně poznal jako food).

Řešení: pro každou dávku nenamatchnutých vstupů si dynamicky vytáhneme jen
ty suroviny z DB, které jsou jim sémanticky nejblíž (cosine similarity), a
TY dáme do promptu. Menší, relevantnější katalog = přesnější odpovědi.

Vyžaduje jednorázový reindex (`python -m app.modules.ingredient_embed`),
než se dá skutečně využít – do té doby `candidates_for_batch` vrací prázdno
a volající (`llm_match.py`) spadne zpátky na statický top-N katalog.
"""
from __future__ import annotations

import logging

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, IngredientEmbedding
from .rag import embed_text  # stejná funkce, stejný model (settings.embed_model)

log = logging.getLogger("kucharka.ingredient_embed")


def reindex(rebuild: bool = False) -> dict:
    """Zembeduj suroviny, které embedding ještě nemají (nebo všechny při rebuild)."""
    db = SessionLocal()
    done = 0
    todo: list = []
    try:
        if rebuild:
            db.query(IngredientEmbedding).delete()
            db.commit()
        have = set(db.scalars(select(IngredientEmbedding.ingredient_id)).all())
        rows = db.scalars(select(Ingredient)).all()
        todo = [i for i in rows if i.id not in have]
        for ing in todo:
            try:
                vec = embed_text(ing.name_cs)
                db.merge(IngredientEmbedding(
                    ingredient_id=ing.id, model=settings.embed_model,
                    dim=int(vec.shape[0]), vec=vec.tobytes(),
                ))
                db.commit()
                done += 1
                if done % 200 == 0:
                    log.info("ingredient embed reindex: %s/%s", done, len(todo))
            except Exception as exc:  # noqa: BLE001
                log.warning("embedding suroviny %s (%s) selhal: %s", ing.id, ing.name_cs, exc)
        log.info("ingredient embed reindex hotovo: %s/%s nových", done, len(todo))
    finally:
        db.close()
    return {"done": done, "total_new": len(todo)}


def _load_matrix(db: Session) -> tuple[list[int], list[str], np.ndarray] | None:
    rows = db.execute(
        select(IngredientEmbedding.ingredient_id, IngredientEmbedding.vec, Ingredient.name_cs)
        .join(Ingredient, Ingredient.id == IngredientEmbedding.ingredient_id)
    ).all()
    if not rows:
        return None
    ids = [r[0] for r in rows]
    names = [r[2] for r in rows]
    mat = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    return ids, names, mat


def candidates_for_batch(db: Session, inputs: list[str], k: int = 20) -> list[tuple[int, str]]:
    """Union nejbližších `k` surovin pro každý vstup v dávce → jeden sdílený
    katalog kandidátů pro tuto dávku (typicky výrazně menší a relevantnější
    než statický top-N, přesto pořád jedno LLM volání na celou dávku).

    Vrátí [] pokud ještě neproběhl reindex (žádné embeddingy v DB) – volající
    by v tom případě měl spadnout zpátky na statický katalog.
    """
    loaded = _load_matrix(db)
    if loaded is None:
        return []
    ids, names, mat = loaded

    picked: dict[int, str] = {}
    for text in inputs:
        try:
            qvec = embed_text(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("embedding vstupu '%s' selhal: %s", text, exc)
            continue
        scores = mat @ qvec
        top = np.argsort(-scores)[:k]
        for i in top:
            picked[ids[i]] = names[i]

    return sorted(picked.items())


def status() -> dict:
    db = SessionLocal()
    try:
        indexed_count = len(db.scalars(select(IngredientEmbedding.ingredient_id)).all())
        total = len(db.scalars(select(Ingredient.id)).all())
        return {
            "indexed": indexed_count > 0,
            "indexed_count": indexed_count,
            "ingredients_total": total,
            "model": settings.embed_model,
        }
    finally:
        db.close()


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
    print(reindex())
