"""Testy průchodu LLM volání přes proxy (ollamaproxy).

Proxy stojí před Ollamou i před komerčními API, sjednocuje je pod jednu
adresu a loguje tokeny a výkon. Autentizace je `Authorization: Bearer opx_…`.

Nativní protokol se ZÁMĚRNĚ zachovává (/api/chat, /api/embed) místo přechodu
na OpenAI-kompatibilní /v1: ta vrstva neumí `num_ctx`, `keep_alive` ani
`format` (JSON schéma). Dávkové úlohy si `num_ctx=8192` nastavují a bez něj
by se jim prompt ořezal – tichá ztráta kvality. Migrace je proto jen
přesměrování adresy plus hlavička s klíčem.

Testy hlídají tři věci:
  * klíč se opravdu posílá na VŠECHNA nativní volání (chat, embed, tags…),
  * bez klíče se hlavička neposílá vůbec (přímá Ollama nesmí dostat cizí),
  * klíč se NIKDY nevrací ven z API ani do logu.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-proxy-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

Base.metadata.create_all(engine)

PASSED = FAILED = 0
KEY = "opx_tajnyklic123"


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


class Recorder:
    """Zachytí odchozí HTTP volání místo skutečné sítě."""

    def __init__(self, payload=None, status=200):
        self.calls = []
        self.payload = payload if payload is not None else {"models": []}
        self.status = status

    def __call__(self, url, **kw):
        self.calls.append({"url": str(url), "headers": dict(kw.get("headers") or {}),
                           "json": kw.get("json")})
        req = httpx.Request("POST", str(url))
        return httpx.Response(self.status, json=self.payload, request=req)

    def auth(self, i=0):
        return self.calls[i]["headers"].get("Authorization")


def with_recorder(fn, *, payload=None, methods=("post", "get")):
    """Spustí fn s podstrčeným httpx a vrátí recorder."""
    rec = Recorder(payload=payload)
    orig = {m: getattr(httpx, m) for m in methods}
    for m in methods:
        setattr(httpx, m, rec)
    try:
        fn()
    except Exception:  # noqa: BLE001 – zajímají nás odchozí hlavičky, ne výsledek
        pass
    finally:
        for m, f in orig.items():
            setattr(httpx, m, f)
    return rec


def main():
    settings.ollama_url = "https://ollamaproxy.example.cz"
    settings.llm_provider = "ollama"
    settings.llm_embed_provider = "ollama"

    # ── bez klíče ──
    print("\nbez klíče (přímá Ollama):")
    settings.llm_proxy_key = ""
    check("hlavičky jsou prázdné", settings.ollama_headers() == {},
          str(settings.ollama_headers()))

    from app.modules import llmclient, ollamachat

    rec = with_recorder(lambda: ollamachat.chat_json_raw(
        settings.ollama_url, "qwen3:4b", "ahoj", timeout=5))
    check("chat volá /api/chat", rec.calls and rec.calls[0]["url"].endswith("/api/chat"),
          str([c["url"] for c in rec.calls]))
    check("bez klíče se Authorization neposílá", rec.auth() is None, str(rec.auth()))

    # ── s klíčem ──
    print("\ns klíčem (přes proxy):")
    settings.llm_proxy_key = KEY
    check("hlavička nese bearer token",
          settings.ollama_headers() == {"Authorization": f"Bearer {KEY}"},
          str(settings.ollama_headers()))

    rec = with_recorder(lambda: ollamachat.chat_json_raw(
        settings.ollama_url, "qwen3:4b", "ahoj", timeout=5))
    check("chat posílá klíč", rec.auth() == f"Bearer {KEY}", str(rec.auth()))

    # Nativní protokol musí zůstat nativní – tohle OpenAI vrstva neumí
    # a dávkové úlohy se na to spoléhají.
    rec = with_recorder(lambda: ollamachat.chat_json_raw(
        settings.ollama_url, "qwen3:4b", "ahoj", timeout=5,
        num_ctx=8192, format_schema={"type": "object"}, keep_alive="30m"))
    body = rec.calls[0]["json"] if rec.calls else {}
    check("num_ctx projde v těle dotazu",
          (body.get("options") or {}).get("num_ctx") == 8192, str(body.get("options")))
    check("keep_alive projde", body.get("keep_alive") == "30m", str(body.get("keep_alive")))
    check("JSON schéma projde jako format", body.get("format") == {"type": "object"},
          str(body.get("format")))

    rec = with_recorder(
        lambda: llmclient.embed_texts(["ahoj"], timeout=5),
        payload={"embeddings": [[0.1, 0.2]]})
    check("embed volá /api/embed",
          rec.calls and rec.calls[0]["url"].endswith("/api/embed"),
          str([c["url"] for c in rec.calls]))
    check("embed posílá klíč", rec.auth() == f"Bearer {KEY}", str(rec.auth()))

    with TestClient(app) as c:
        rec = with_recorder(lambda: c.get("/api/admin/test-ollama"),
                            payload={"models": [{"name": "qwen3:4b"}]})
        tags = [x for x in rec.calls if x["url"].endswith("/api/tags")]
        check("test spojení volá /api/tags", bool(tags), str([x["url"] for x in rec.calls]))
        check("test spojení posílá klíč",
              tags and tags[0]["headers"].get("Authorization") == f"Bearer {KEY}",
              str(tags[0]["headers"]) if tags else "žádné volání")

        # ── seznam modelů pro výběr v administraci ──
        # Formulář na něm staví nabídku modelů. Když se seznam nenačte, MUSÍ
        # se to poznat: administrace se pak přepne na volný text, aby se
        # nastavení dalo opravit i s nepojízdnou proxy.
        print("\nseznam modelů pro nastavení:")
        out = {}
        rec = with_recorder(
            lambda: out.update(c.get("/api/admin/models").json()),
            payload={"models": [{"name": "qwen3:8b"}, {"name": "aya:8b"},
                                {"name": "qwen3:8b"}, {"name": ""}]})
        tags = [x for x in rec.calls if x["url"].endswith("/api/tags")]
        check("seznam se ptá na /api/tags", bool(tags), str([x["url"] for x in rec.calls]))
        check("seznam posílá klíč",
              tags and tags[0]["headers"].get("Authorization") == f"Bearer {KEY}",
              str(tags[0]["headers"]) if tags else "žádné volání")
        check("jména jdou setříděná a bez duplicit",
              out.get("models") == ["aya:8b", "qwen3:8b"], str(out.get("models")))
        check("při úspěchu se nehlásí chyba", "error" not in out, str(out))

        def boom(url, **kw):
            raise httpx.ConnectError("proxy neodpovídá")

        orig_get = httpx.get
        httpx.get = boom
        try:
            out = c.get("/api/admin/models").json()
        finally:
            httpx.get = orig_get
        check("nedostupná proxy neshodí administraci",
              out.get("models") == [] and bool(out.get("error")), str(out))
        check("chyba neprozradí klíč", KEY not in str(out), str(out))

        # ── klíč se nesmí dostat ven ──
        print("\nklíč se nedostane ven:")
        body = c.get("/api/admin/settings").json()
        check("nastavení klíč nevrací", "llm_proxy_key" not in body, str(list(body)[:5]))
        check("vrací se jen příznak, že je nastavený",
              body.get("llm_proxy_key_set") is True, str(body.get("llm_proxy_key_set")))
        check("klíč není nikde v odpovědi", KEY not in c.get("/api/admin/settings").text)

        # ── uložení a zapomenutí ──
        print("\nuložení a zapomenutí:")
        r = c.put("/api/admin/settings", json={"values": {"llm_proxy_key": "opx_novy"}})
        check("klíč jde uložit", r.status_code == 200 and settings.llm_proxy_key == "opx_novy",
              settings.llm_proxy_key)

        c.put("/api/admin/settings", json={"values": {"llm_proxy_key": ""}})
        check("prázdná hodnota klíč NEMAŽE (formulář ho nikdy nedostane zpět)",
              settings.llm_proxy_key == "opx_novy", settings.llm_proxy_key)

        c.put("/api/admin/settings", json={"values": {"llm_proxy_key_clear": True}})
        check("výslovné zapomenutí klíč smaže", settings.llm_proxy_key == "",
              settings.llm_proxy_key)
        check("a komerční klíč to nezasáhne",
              c.get("/api/admin/settings").json().get("llm_api_key_set") is not None)

    settings.llm_proxy_key = ""
    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
