"""Testy směrování podle modelu: text, OCR (obrázky) a embeddingy.

Přepínač poskytovatele není – každé políčko modelu může ukazovat na lokální
Ollamu nebo komerčního poskytovatele a cesta se volí podle katalogu proxy
(/mgmt/v1/models). Testy katalog podstrčí přímo do cache; žádná síť.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-providers-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

import app.models  # noqa: E402,F401 - naplní metadata před create_all
from app.config import settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.modules import llmclient, proxy_catalog  # noqa: E402

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


FAKE_CATALOG = {
    "ollama": {"kind": "ollama", "base_url": "https://api.example.com/v1",
               "models": ["gemma4:12b", "nomic-embed-text:latest"], "ok": True, "error": None},
    "openai": {"kind": "openai", "base_url": "https://api.example.com/providers/openai/v1",
               "models": ["gpt-4o-mini", "text-embedding-3-small"], "ok": True, "error": None},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.example.com/providers/anthropic",
                  "models": ["claude-haiku-5-5"], "ok": True, "error": None},
}


def with_api(**over):
    """Nastaví proxy (klíč + katalog) a vrátí funkci pro obnovení původního stavu.

    Komerční cesta jde vždy přes proxy: adresa je OLLAMA_URL + /v1 a klíč
    je klíč proxy (settings.api_url / settings.api_key).
    """
    keys = ("llm_proxy_key", "ocr_model", "embed_model", "ollama_url", "_fast_model", "ollama_model")
    old = {k: getattr(settings, k) for k in keys}
    old_cache = dict(proxy_catalog._cache)
    settings.llm_proxy_key = "sk-test"
    settings.ollama_url = "https://api.example.com"
    proxy_catalog._cache.update(ts=time.time(), backends=FAKE_CATALOG, error=None)
    for k, v in over.items():
        setattr(settings, k, v)

    def restore():
        for k, v in old.items():
            setattr(settings, k, v)
        proxy_catalog._cache.update(old_cache)
    return restore


def main():
    # ── výchozí stav: všechno lokálně ──────────────────────────────────
    check("bez katalogu je textový model lokální", not settings.llm_api_enabled)
    check("bez katalogu je OCR model lokální", not settings.llm_vision_api_enabled)
    check("bez katalogu je embed model lokální", not settings.llm_embed_api_enabled)
    restore = with_api(ocr_model="gemma4:12b", embed_model="nomic-embed-text")
    try:
        check("lokální model z katalogu není komerční", not settings.llm_vision_api_enabled)
        check("`nomic-embed-text` == `nomic-embed-text:latest`", not settings.llm_embed_api_enabled)
        check("model mimo katalog se bere jako lokální", not proxy_catalog.is_remote("neznamy:1b"))
        check("claude je komerční", proxy_catalog.is_remote("claude-haiku-5-5"))
        check("anthropic jde na /v1/messages",
              proxy_catalog.backend_for("claude-haiku-5-5") == ("anthropic", "https://api.example.com/providers/anthropic"))
    finally:
        restore()

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

    restore = with_api(ocr_model="gpt-4o-mini")
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
    restore = with_api(ocr_model="")
    try:
        err = llmclient.vision_error()
        check("bez OCR modelu je hláška, ne výjimka", err and "OCR model" in err, str(err))
        out, raw = llmclient.vision_json("x", images=["QUJD"])
        check("nedostupné OCR vrací None", out is None and raw.startswith("<"))
    finally:
        restore()

    # ── embeddingy přes komerční API ───────────────────────────────────
    restore = with_api(embed_model="text-embedding-3-small")
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

    check("aktivní embed model je vždy embed_model",
          llmclient.active_embed_model() == settings.embed_model)

    # ── Anthropic: nativní Messages API, obrázek jako base64 blok ──────
    restore = with_api(ocr_model="claude-haiku-5-5")
    orig_post = llmclient.httpx.post
    try:
        def fake_anthropic(url, json=None, headers=None, timeout=None):
            seen["url"] = url
            seen["json"] = json
            return FakeResp({"content": [{"type": "text", "text": '{"items": ["sýr"]}'}],
                             "usage": {"input_tokens": 9, "output_tokens": 4}})
        llmclient.httpx.post = fake_anthropic
        out, raw = llmclient.vision_json("přečti", images=["QUJD"], timeout=5)
        check("anthropic OCR vrátí JSON", out == {"items": ["sýr"]}, str(out))
        check("anthropic jde na /v1/messages", seen["url"].endswith("/providers/anthropic/v1/messages"), seen["url"])
        blocks = seen["json"]["messages"][0]["content"]
        check("obrázek je base64 blok", blocks[1]["type"] == "image" and blocks[1]["source"]["data"] == "QUJD")
        check("max_tokens je nastavené", seen["json"].get("max_tokens", 0) > 0)
    finally:
        llmclient.httpx.post = orig_post
        restore()

    # ── nesouhlas počtu vektorů se pozná ───────────────────────────────
    restore = with_api(embed_model="text-embedding-3-small")
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
