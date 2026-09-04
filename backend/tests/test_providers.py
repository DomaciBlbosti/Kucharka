"""Testy přepínání providerů: text, OCR (obrázky) a embeddingy.

Každá oblast má vlastní přepínač a výchozí stav je „ollama", takže se nic
nezmění, dokud se to ručně nepřepne. Testy jdou přes fake HTTP vrstvu –
žádná síť, žádná Ollama.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-providers-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

import app.models  # noqa: E402,F401 - naplní metadata před create_all
from app.config import settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.modules import llmclient  # noqa: E402

Base.metadata.create_all(engine)

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def with_api(**over):
    """Nastaví komerční API a vrátí funkci pro obnovení původního stavu."""
    keys = ("llm_provider", "llm_vision_provider", "llm_embed_provider",
            "llm_api_key", "llm_api_url", "llm_api_model",
            "llm_api_vision_model", "llm_api_embed_model", "ocr_model",
            "embed_model", "ollama_url")
    old = {k: getattr(settings, k) for k in keys}
    settings.llm_api_key = "sk-test"
    settings.llm_api_url = "https://api.example.com/v1"
    for k, v in over.items():
        setattr(settings, k, v)
    return lambda: [setattr(settings, k, v) for k, v in old.items()]


def main():
    # ── výchozí stav: všechno lokálně ──────────────────────────────────
    check("výchozí provider textu je ollama", settings.llm_provider == "ollama")
    check("výchozí provider OCR je ollama", settings.llm_vision_provider == "ollama")
    check("výchozí provider embeddingů je ollama", settings.llm_embed_provider == "ollama")
    check("samotný API klíč OCR nepřepne", not settings.llm_vision_api_enabled)

    # ── OCR přes komerční API ──────────────────────────────────────────
    seen: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        if url.endswith("/embeddings"):
            return FakeResp({
                "data": [{"index": 1, "embedding": [0.0, 1.0]},
                         {"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 12},
            })
        return FakeResp({
            "choices": [{"message": {"content": '{"items": ["mléko"]}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 7},
        })

    restore = with_api(llm_vision_provider="api", llm_api_vision_model="gpt-4o-mini")
    orig_post = llmclient.httpx.post
    try:
        llmclient.httpx.post = fake_post
        check("OCR přes API je dostupné", llmclient.vision_error() is None)
        out, raw = llmclient.vision_json("přečti", images=["QUJD"], timeout=5)
        check("OCR vrátí naparsovaný JSON", out == {"items": ["mléko"]}, str(out))
        content = seen["json"]["messages"][0]["content"]
        check("obrázek jde jako data URL",
              content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,QUJD"),
              str(content[1]))
        check("prompt je v obsahu jako text", content[0] == {"type": "text", "text": "přečti"})
        check("použije se vision model", seen["json"]["model"] == "gpt-4o-mini")
        check("klíč jde v hlavičce", seen["headers"]["Authorization"] == "Bearer sk-test")
    finally:
        llmclient.httpx.post = orig_post
        restore()

    # OCR zpět na Ollamu bez modelu → srozumitelná hláška, ne pád
    restore = with_api(llm_vision_provider="ollama", ocr_model="", ollama_url="http://x")
    try:
        err = llmclient.vision_error()
        check("bez OCR modelu je hláška, ne výjimka", err and "OCR model" in err, str(err))
        out, raw = llmclient.vision_json("x", images=["QUJD"])
        check("nedostupné OCR vrací None", out is None and raw.startswith("<"))
    finally:
        restore()

    # ── embeddingy přes komerční API ───────────────────────────────────
    restore = with_api(llm_embed_provider="api", llm_api_embed_model="text-embedding-3-small")
    orig_post = llmclient.httpx.post
    try:
        llmclient.httpx.post = fake_post
        check("aktivní embed model je API model",
              llmclient.active_embed_model() == "text-embedding-3-small")
        vecs = llmclient.embed_texts(["a", "b"])
        check("embeddingy jdou na /embeddings", seen["url"].endswith("/embeddings"), seen["url"])
        check("vrátí se vektory v pořadí vstupů",
              vecs == [[1.0, 0.0], [0.0, 1.0]], str(vecs))
        check("celá dávka jedním voláním", seen["json"]["input"] == ["a", "b"])
    finally:
        llmclient.httpx.post = orig_post
        restore()

    check("aktivní embed model bez přepnutí je lokální",
          llmclient.active_embed_model() == settings.embed_model)

    # ── nesouhlas počtu vektorů se pozná ───────────────────────────────
    restore = with_api(llm_embed_provider="api")
    orig_post = llmclient.httpx.post
    try:
        llmclient.httpx.post = lambda *a, **k: FakeResp(
            {"data": [{"index": 0, "embedding": [1.0]}], "usage": {}}
        )
        try:
            llmclient.embed_texts(["a", "b"])
            check("chybný počet vektorů vyhodí výjimku", False, "neprošlo")
        except Exception as exc:  # noqa: BLE001
            check("chybný počet vektorů vyhodí výjimku", "nesedí" in str(exc), str(exc))
    finally:
        llmclient.httpx.post = orig_post
        restore()

    # ── RAG index nemíchá vektory z různých modelů ─────────────────────
    from app.models import Recipe, RecipeEmbedding
    from app.db import SessionLocal
    from app.modules import rag

    db = SessionLocal()
    try:
        r = Recipe(title="Test", source_url="http://t/1", source_domain="t.cz")
        db.add(r)
        db.flush()
        import numpy as np
        db.add(RecipeEmbedding(recipe_id=r.id, model="cizi-model", dim=3,
                               vec=np.zeros(3, dtype=np.float32).tobytes()))
        db.commit()
        st = rag.index_status()
        check("vektor z jiného modelu se nepočítá jako indexovaný", st["indexed"] == 0,
              str(st["indexed"]))
        check("ale je vidět, že čeká na přeindexování",
              st["indexed_other_model"] == 1, str(st.get("indexed_other_model")))
        check("matice se nesestaví z cizích vektorů", rag._load_matrix(db) is None)
    finally:
        db.close()

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
