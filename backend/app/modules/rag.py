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
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe, RecipeEmbedding
from .ingest import _persist
from .ollamachat import chat_json

log = logging.getLogger("kucharka.rag")

_lock = threading.Lock()
_index_state: dict = {"running": False, "done": 0, "total": 0, "finished_at": None}


# ----------------------------- embedding -----------------------------
def embed_text(text: str) -> np.ndarray:
    r = httpx.post(
        f"{settings.ollama_url}/api/embeddings",
        json={"model": settings.embed_model, "prompt": text},
        timeout=60,
    )
    r.raise_for_status()
    vec = np.asarray(r.json()["embedding"], dtype=np.float32)
    n = np.linalg.norm(vec)
    return vec / n if n else vec  # normalizace → kosinus = dot


def embed_texts_batch(texts: list[str], timeout: float = 120, retries: int = 3) -> list[np.ndarray]:
    """Zaembeduj víc textů JEDNÍM HTTP voláním (novější `/api/embed`, ne
    `/api/embeddings`) – zásadně méně round-tripů než volat `embed_text` v
    cyklu.

    Pozorováno v provozu: volání občas selže (500/501) i na endpointu, který
    o chvíli později (nebo při ručním testu) projde bez problému – vypadá to
    na přechodnou nespolehlivost proxy mezi appkou a Ollamou, ne na trvalou
    nekompatibilitu. Proto pár rychlých pokusů s krátkou prodlevou, než se
    to vzdá a spadne na sekvenční `embed_text` (což taky zkusí víckrát).
    """
    if not texts:
        return []
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = httpx.post(
                f"{settings.ollama_url}/api/embed",
                json={"model": settings.embed_model, "input": texts},
                timeout=timeout,
            )
            r.raise_for_status()
            vecs = r.json()["embeddings"]
            if len(vecs) != len(texts):
                raise ValueError(f"počet vektorů ({len(vecs)}) nesedí na počet vstupů ({len(texts)})")
            out = []
            for v in vecs:
                arr = np.asarray(v, dtype=np.float32)
                n = np.linalg.norm(arr)
                out.append(arr / n if n else arr)
            return out
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    log.warning(
        "dávkový /api/embed selhal %sx (%s), fallback na sekvenční volání", retries, last_exc,
    )
    return _embed_texts_sequential(texts)


def _embed_texts_sequential(texts: list[str], retries: int = 2) -> list[np.ndarray]:
    """Fallback po jednom, taky s pár pokusy na položku – jedna trvale
    padající surovina by jinak strhla celou dávku (viz `embed_text`)."""
    out = []
    for t in texts:
        vec = None
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                vec = embed_text(t)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(1.0)
        if vec is None:
            log.warning("embedding '%s' selhal i po %sx pokusech: %s", t, retries, last_exc)
            raise last_exc  # zachovej původní chování - volající (candidates_for_batch) to odchytí
        out.append(vec)
    return out


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
        s["indexed"] = db.scalar(
            select(func.count(RecipeEmbedding.recipe_id))
        ) or 0
        s["recipes_total"] = db.scalar(select(func.count(Recipe.id))) or 0
    finally:
        db.close()
    s["model"] = settings.embed_model
    return s


def index_recipes(rebuild: bool = False, batch_log: int = 50) -> dict:
    """Zembedduj recepty, které ještě embedding nemají (nebo všechny při rebuild)."""
    db = SessionLocal()
    try:
        if rebuild:
            db.query(RecipeEmbedding).delete()
            db.commit()
        have = set(db.scalars(select(RecipeEmbedding.recipe_id)).all())
        recipes = db.scalars(select(Recipe)).all()
        todo = [r for r in recipes if r.id not in have]
        with _lock:
            _index_state.update(running=True, done=0, total=len(todo), finished_at=None)
        for i, r in enumerate(todo, 1):
            try:
                vec = embed_text(recipe_doc(r))
                db.merge(RecipeEmbedding(
                    recipe_id=r.id, model=settings.embed_model,
                    dim=int(vec.shape[0]), vec=vec.tobytes(),
                ))
                db.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("embedding receptu %s selhal: %s", r.id, exc)
            with _lock:
                _index_state["done"] = i
            if i % batch_log == 0:
                log.info("indexace %s/%s", i, len(todo))
    finally:
        with _lock:
            _index_state.update(running=False, finished_at=time.time())
        db.close()
    return index_status()


def index_async(rebuild: bool = False) -> bool:
    with _lock:
        if _index_state["running"]:
            return False
    threading.Thread(target=index_recipes, kwargs={"rebuild": rebuild}, daemon=True).start()
    return True


# ----------------------------- vyhledání -----------------------------
def search(
    db: Session,
    query: str,
    k: int = 6,
    max_kcal: float | None = None,
    min_rating: float | None = None,
) -> list[tuple[Recipe, float]]:
    qvec = embed_text(query)

    stmt = select(Recipe.id).join(RecipeEmbedding, RecipeEmbedding.recipe_id == Recipe.id)
    if max_kcal is not None:
        stmt = stmt.where(Recipe.kcal_per_serving.isnot(None),
                          Recipe.kcal_per_serving <= max_kcal)
    if min_rating is not None:
        stmt = stmt.where(Recipe.rating.isnot(None), Recipe.rating >= min_rating)
    allowed = set(db.scalars(stmt).all())
    if not allowed:
        return []

    rows = db.execute(
        select(RecipeEmbedding.recipe_id, RecipeEmbedding.vec)
        .where(RecipeEmbedding.recipe_id.in_(allowed))
    ).all()
    if not rows:
        return []

    ids = [rid for rid, _ in rows]
    mat = np.stack([np.frombuffer(v, dtype=np.float32) for _, v in rows])
    scores = mat @ qvec  # vektory jsou normalizované → kosinus
    top = np.argsort(-scores)[:k]
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
    out = chat_json(
        settings.ollama_url,
        settings.ollama_model,
        sys_prompt,
        timeout=max(settings.http_timeout, 180),
        temperature=0.7,
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
