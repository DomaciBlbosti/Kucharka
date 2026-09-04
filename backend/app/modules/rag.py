"""RAG nad recepty: embedding do MariaDB + vektorové vyhledání + generování.

Žádná externí vektorová DB – embeddingy (nomic-embed-text) se ukládají jako
float32 bytes k receptu a podobnost se počítá v numpy (pro pár tisíc receptů
je brute-force kosinová podobnost okamžitá). Filtrování podle kalorií/hodnocení
řeší SQL nad tabulkou recipe.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

import httpx
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe, RecipeEmbedding
from .ingest import _persist

log = logging.getLogger("kucharka.rag")

_lock = threading.Lock()
_index_state: dict = {"running": False, "done": 0, "total": 0, "finished_at": None}

# Matice embeddingů se drží v paměti mezi dotazy (načtení stovek MB blobů
# z DB při KAŽDÉM generování bylo při růstu DB neúnosné). Invalidace přes
# počet řádků – po doindexování se počet změní a matice se načte znovu.
_matrix_cache: dict = {"count": -1, "ids": None, "mat": None, "model": None}
_matrix_lock = threading.Lock()


# ----------------------------- embedding -----------------------------
def _normalize(v) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(arr)
    return arr / n if n else arr  # normalizace → kosinus = dot


def embed_text(text: str) -> np.ndarray:
    """Jeden text. Provider řeší llmclient (lokální Ollama, nebo komerční API)."""
    from . import llmclient

    return _normalize(llmclient.embed_texts([text])[0])


def embed_texts_batch(texts: list[str], timeout: float = 60, retries: int = 2) -> list[np.ndarray]:
    """Zaembeduj víc textů JEDNÍM HTTP voláním (novější `/api/embed`, ne
    `/api/embeddings`) – zásadně méně round-tripů než volat `embed_text` v
    cyklu.

    Krátký retry (ne agresivní) – vytrvalé selhání řeší circuit breaker v
    `ingredient_embed.py`, ne opakování tady. Moc pokusů na volání by při
    tisících dávek znamenalo hodiny čekání, než se cokoliv reálně napáruje.
    """
    if not texts:
        return []
    from . import llmclient

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return [_normalize(v) for v in llmclient.embed_texts(texts, timeout=timeout)]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.0)
    log.warning(
        "dávkový /api/embed selhal %sx (%s), fallback na sekvenční volání", retries, last_exc,
    )
    return _embed_texts_sequential(texts)


def _embed_texts_sequential(texts: list[str]) -> list[np.ndarray]:
    """Fallback po jednom, BEZ retry na položku – jeden pokus stačí, o
    vytrvalé selhání se stará circuit breaker o úroveň výš, ne opakování
    tady (u 40 položek by retry na každou znamenalo desítky sekund navíc)."""
    return [embed_text(t) for t in texts]


def recipe_doc(r: Recipe) -> str:
    """Textová reprezentace receptu pro embedding."""
    ings = ", ".join(
        (ri.ingredient.name_cs if ri.ingredient else ri.raw_text) for ri in r.ingredients
    )
    parts = [r.title]
    if r.category:
        parts.append(r.category)
    if ings:
        parts.append("Suroviny: " + ings)
    if r.kcal_per_serving:
        parts.append(f"{round(r.kcal_per_serving)} kcal na porci")
    return ". ".join(parts)


# ----------------------------- indexace -----------------------------
def index_status() -> dict:
    with _lock:
        s = dict(_index_state)
    db = SessionLocal()
    try:
        from . import llmclient

        model = llmclient.active_embed_model()
        s["indexed"] = db.scalar(
            select(func.count(RecipeEmbedding.recipe_id))
            .where(RecipeEmbedding.model == model)
        ) or 0
        # vektory z JINÉHO modelu (po přepnutí provideru) – jsou k ničemu,
        # dokud se nepřeindexuje, tak ať je to v administraci vidět
        s["indexed_other_model"] = (db.scalar(
            select(func.count(RecipeEmbedding.recipe_id))
            .where(RecipeEmbedding.model != model)
        ) or 0)
        s["recipes_total"] = db.scalar(select(func.count(Recipe.id))) or 0
    finally:
        db.close()
    s["model"] = model
    return s


def _candidate_ids(db: Session) -> list[int]:
    """Recepty, které má smysl indexovat, do stropu `rag_max_recipes`.

    U stovek tisíc receptů nejde (a nedává smysl) embeddovat všechno – index
    by se počítal dny a matice žrala stovky MB RAM. Bereme nejkvalitnější
    podmnožinu: vlastní hodnocení > hodnocení webu > novější recepty.
    """
    stmt = select(Recipe.id).order_by(
        func.coalesce(Recipe.user_rating, 0).desc(),
        func.coalesce(Recipe.rating, 0).desc(),
        Recipe.id.desc(),
    )
    limit = settings.rag_max_recipes
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def index_recipes(rebuild: bool = False, chunk_size: int = 32) -> dict:
    """Zembedduj kandidátní recepty bez embeddingu (při rebuild všechny znovu).

    Dávkově po `chunk_size` přes /api/embed – řádově rychlejší než volání
    po jednom. Recepty se načítají po chunkách, ne celá DB do paměti.
    """
    from . import llmclient

    model = llmclient.active_embed_model()
    db = SessionLocal()
    try:
        if rebuild:
            db.query(RecipeEmbedding).delete()
            db.commit()
        # jen vektory z AKTIVNÍHO modelu; po přepnutí provideru (jiný rozměr)
        # se index doplní znovu místo míchání nekompatibilních vektorů
        have = set(db.scalars(
            select(RecipeEmbedding.recipe_id).where(RecipeEmbedding.model == model)
        ).all())
        todo_ids = [rid for rid in _candidate_ids(db) if rid not in have]
        with _lock:
            _index_state.update(running=True, done=0, total=len(todo_ids), finished_at=None)
        done = 0
        for start in range(0, len(todo_ids), chunk_size):
            chunk_ids = todo_ids[start:start + chunk_size]
            recipes = db.scalars(
                select(Recipe)
                .where(Recipe.id.in_(chunk_ids))
                .options(selectinload(Recipe.ingredients))
            ).all()
            try:
                vecs = embed_texts_batch([recipe_doc(r) for r in recipes], timeout=120)
            except Exception as exc:  # noqa: BLE001
                log.warning("embedding dávky receptů selhal: %s", exc)
                done += len(chunk_ids)
                with _lock:
                    _index_state["done"] = done
                continue
            for r, vec in zip(recipes, vecs):
                db.merge(RecipeEmbedding(
                    recipe_id=r.id, model=llmclient.active_embed_model(),
                    dim=int(vec.shape[0]), vec=vec.tobytes(),
                ))
            db.commit()
            done += len(chunk_ids)
            with _lock:
                _index_state["done"] = done
            if (start // chunk_size) % 20 == 0 and start:
                log.info("indexace %s/%s", done, len(todo_ids))
    finally:
        with _lock:
            _index_state.update(running=False, finished_at=time.time())
        with _matrix_lock:
            _matrix_cache.update(count=-1, ids=None, mat=None, model=None)  # invalidace
        db.close()
    return index_status()


def index_async(rebuild: bool = False) -> bool:
    with _lock:
        if _index_state["running"]:
            return False
    threading.Thread(target=index_recipes, kwargs={"rebuild": rebuild}, daemon=True).start()
    return True


# ----------------------------- vyhledání -----------------------------
def _load_matrix(db: Session) -> tuple[list[int], np.ndarray] | None:
    """Vrátí (ids, matice) z cache; při změně počtu embeddingů se načte znovu."""
    from . import llmclient

    model = llmclient.active_embed_model()
    count = db.scalar(
        select(func.count(RecipeEmbedding.recipe_id)).where(RecipeEmbedding.model == model)
    ) or 0
    if count == 0:
        return None
    with _matrix_lock:
        if _matrix_cache["count"] == count and _matrix_cache["model"] == model:
            return _matrix_cache["ids"], _matrix_cache["mat"]
    # Jen vektory aktivního modelu: po přepnutí provideru mají staré vektory
    # jiný rozměr a np.stack by na míchání spadl.
    rows = db.execute(
        select(RecipeEmbedding.recipe_id, RecipeEmbedding.vec)
        .where(RecipeEmbedding.model == model)
    ).all()
    ids = [rid for rid, _ in rows]
    mat = np.stack([np.frombuffer(v, dtype=np.float32) for _, v in rows])
    with _matrix_lock:
        _matrix_cache.update(count=len(ids), ids=ids, mat=mat, model=model)
    return ids, mat


def search(
    db: Session,
    query: str,
    k: int = 6,
    max_kcal: float | None = None,
    min_rating: float | None = None,
) -> list[tuple[Recipe, float]]:
    qvec = embed_text(query)
    loaded = _load_matrix(db)
    if loaded is None:
        return []
    ids, mat = loaded

    # Filtry (kcal/hodnocení) řeší SQL; skóre se pak jen maskuje – matice
    # zůstává sdílená v cache, nic se nekopíruje per dotaz.
    allowed: set[int] | None = None
    if max_kcal is not None or min_rating is not None:
        stmt = select(Recipe.id).join(RecipeEmbedding, RecipeEmbedding.recipe_id == Recipe.id)
        if max_kcal is not None:
            stmt = stmt.where(Recipe.kcal_per_serving.isnot(None),
                              Recipe.kcal_per_serving <= max_kcal)
        if min_rating is not None:
            stmt = stmt.where(Recipe.rating.isnot(None), Recipe.rating >= min_rating)
        allowed = set(db.scalars(stmt).all())
        if not allowed:
            return []

    scores = mat @ qvec  # vektory jsou normalizované → kosinus
    if allowed is not None:
        mask = np.fromiter((rid in allowed for rid in ids), dtype=bool, count=len(ids))
        scores = np.where(mask, scores, -np.inf)
    top = [i for i in np.argsort(-scores)[:k] if np.isfinite(scores[i])]
    top_ids = [ids[i] for i in top]
    recipes = {r.id: r for r in db.scalars(select(Recipe).where(Recipe.id.in_(top_ids)))}
    return [(recipes[ids[i]], float(scores[i])) for i in top if ids[i] in recipes]


# ----------------------------- generování -----------------------------
def generate(
    db: Session,
    prompt: str,
    k: int | None = None,
    max_kcal: float | None = None,
    min_rating: float | None = None,
) -> dict:
    k = k or settings.rag_k
    hits = search(db, prompt, k=k, max_kcal=max_kcal, min_rating=min_rating)

    context = []
    for r, _ in hits:
        ings = "; ".join(
            (ri.ingredient.name_cs if ri.ingredient else ri.raw_text)
            for ri in r.ingredients
        )
        kcal = f"{round(r.kcal_per_serving)} kcal/porce" if r.kcal_per_serving else "?"
        context.append(f"- {r.title} ({kcal}). Suroviny: {ings}")
    context_block = "\n".join(context) if context else "(žádné podobné recepty)"

    limits = []
    if max_kcal:
        limits.append(f"maximálně {int(max_kcal)} kcal na porci")
    if min_rating:
        limits.append(f"inspiruj se hlavně dobře hodnocenými recepty")
    limit_block = ("Omezení: " + ", ".join(limits) + ".") if limits else ""

    sys_prompt = (
        "Jsi zkušený kuchař. Na základě existujících receptů níže vymysli JEDEN "
        "nový, smysluplný recept podle zadání. Vyjdi z nich stylem a surovinami, "
        "ale vytvoř původní recept (ne kopii). Měrné jednotky uváděj metricky (g, "
        "ml, ks, lžíce). Odpověz POUZE JSON objektem bez dalšího textu:\n"
        '{"title": string, "servings": number, "total_time": number, '
        '"kcal_per_serving": number, "ingredients": [string], "steps": [string], '
        '"note": string}\n\n'
        f"Zadání: {prompt}\n{limit_block}\n\n"
        f"Existující recepty pro inspiraci:\n{context_block}"
    )
    from . import llmclient

    out = llmclient.structured_json(
        sys_prompt,
        timeout=max(settings.http_timeout, 180),
        temperature=0.7,
        # generování je kreativní úloha → hlavní (větší) model, ne rychlý
        ollama_model=settings.ollama_model,
        component="generování",
    )
    if out is None:
        raise RuntimeError("generování receptu selhalo (volání modelu nebo parsování)")
    data = out

    data.setdefault("ingredients", [])
    data.setdefault("steps", [])
    return {
        "recipe": data,
        "sources": [
            {"id": r.id, "title": r.title, "domain": r.source_domain,
             "kcal_per_serving": r.kcal_per_serving, "score": round(score, 3)}
            for r, score in hits
        ],
    }


def save_generated(db: Session, gen: dict) -> Recipe:
    """Ulož vygenerovaný recept do DB (projde normalizací → cook-meter, kcal)."""
    steps = gen.get("steps") or []
    instructions = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) if steps else None
    data = {
        "title": gen.get("title", "Vymyšlený recept"),
        "source_url": f"ai://{uuid.uuid4()}",
        "source_domain": "ai",
        "image_url": None,
        "video_url": None,
        "instructions": instructions,
        "servings": gen.get("servings"),
        "total_time": gen.get("total_time"),
        "rating": None,
        "rating_count": None,
        "category": "Vymyšlené",
        "ingredients": [str(x) for x in gen.get("ingredients", [])],
    }
    return _persist(db, data)
