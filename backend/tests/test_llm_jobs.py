"""Testy fronty odložených úloh na proxy (POST /mgmt/v1/jobs).

Proč fronta: dávkové úlohy posílají na GPU stovky dotazů. Vlastní zámek uměl
jen „jeden po druhém" – nevěděl nic o tom, jaký model je nahraný, a hlavně
nedokázal pustit napřed interaktivní dotaz (recept z fotky, OCR), který
uživatel čeká teď hned. Proxy úlohy seskupí podle modelu a interaktivní
dotazy pustí přednostně.

Testy stojí na falešné proxy, protože skutečná potřebuje klíč. Hlídají hlavně
to, co by se v provozu poznalo nejhůř:

  * pořadí výsledků odpovídá pořadí vstupů (volající je páruje podle indexu),
  * výpadek proxy dávkovou úlohu NEZABIJE – spadne se na přímé volání,
  * nekompletní odpověď fronty se bere jako neúspěch, ne jako posunutá data,
  * tělo dotazu je stejné jako u přímého volání (num_ctx, format, keep_alive).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-jobs-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.models import LlmCall  # noqa: E402,F401 – ať create_all založí i tabulku telemetrie
from app.modules import llmclient, llmjobs, ollamachat  # noqa: E402

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


def _answer(text: str) -> dict:
    """Odpověď Ollamy tak, jak ji fronta vrátí v `result`."""
    return {"message": {"content": text},
            "prompt_eval_count": 11, "eval_count": 7}


class FakeProxy:
    """Fronta v paměti. `behaviour` říká, jak se má chovat."""

    def __init__(self, behaviour="ok", pending_rounds=0):
        self.behaviour = behaviour
        self.pending_rounds = pending_rounds
        self.submitted = []      # těla dotazů, jak dorazila
        self.submit_payloads = []
        self.polls = 0
        self.deleted = []

    # -- odchozí HTTP --
    def post(self, url, **kw):
        if self.behaviour == "submit_fails":
            raise httpx.ConnectError("proxy neodpovídá")
        payload = kw.get("json") or {}
        self.submit_payloads.append(payload)
        for j in payload.get("jobs", []):
            self.submitted.append(j)
        body = {"batch_id": "b1"} if self.behaviour != "no_batch_id" else {
            "jobs": [{"id": i} for i in range(len(payload.get("jobs", [])))]}
        return httpx.Response(202, json=body,
                              request=httpx.Request("POST", str(url)))

    def get(self, url, **kw):
        self.polls += 1
        if self.behaviour == "poll_fails":
            raise httpx.ConnectError("proxy spadla během čekání")
        n = len(self.submitted)
        if self.behaviour == "short_result":
            n = max(0, n - 1)          # jedna úloha se ztratila
        jobs = []
        for i in range(n):
            still = self.polls <= self.pending_rounds
            jobs.append({
                "id": i,
                "status": "queued" if still else "done",
                "result": None if still else _answer(json.dumps({"i": i})),
            })
        if self.behaviour == "job_error" and jobs and not (self.polls <= self.pending_rounds):
            jobs[0] = {"id": 0, "status": "error", "result": None}
        return httpx.Response(200, json=jobs,
                              request=httpx.Request("GET", str(url)))

    def delete(self, url, **kw):
        self.deleted.append(str(url))
        return httpx.Response(200, json={}, request=httpx.Request("DELETE", str(url)))


def with_proxy(proxy, fn):
    orig = (httpx.post, httpx.get, httpx.delete)
    httpx.post, httpx.get, httpx.delete = proxy.post, proxy.get, proxy.delete
    try:
        return fn()
    finally:
        httpx.post, httpx.get, httpx.delete = orig


def main():
    settings.ollama_url = "https://proxy.example.cz"
    settings.llm_provider = "ollama"
    llmjobs._POLL_START_S = 0.01     # ať test nečeká reálné vteřiny
    llmjobs._POLL_MAX_S = 0.02

    # ── kdy je fronta vůbec v provozu ──
    print("\nzapnutí fronty:")
    settings.llm_proxy_key = ""
    settings.llm_jobs_enabled = True
    check("bez klíče proxy je fronta vypnutá", llmjobs.enabled() is False)
    settings.llm_proxy_key = "opx_test"
    settings.llm_jobs_enabled = False
    check("vypnutá volba znamená vypnutou frontu", llmjobs.enabled() is False)
    settings.llm_jobs_enabled = True
    check("s klíčem a zapnutou volbou fronta jede", llmjobs.enabled() is True)

    # ── dávka projde a pořadí sedí ──
    print("\ndávka:")
    proxy = FakeProxy()
    outs = with_proxy(proxy, lambda: llmclient.structured_json_many(
        [f"dotaz {i}" for i in range(4)], schema={"type": "object"},
        num_ctx=8192, component="test"))
    check("vrátí se tolik výsledků, kolik bylo dotazů", len(outs) == 4, str(len(outs)))
    check("pořadí výsledků odpovídá pořadí vstupů",
          [o.get("i") for o in outs] == [0, 1, 2, 3], str(outs))
    check("celá dávka odešla JEDNÍM voláním", len(proxy.submit_payloads) == 1,
          str(len(proxy.submit_payloads)))
    check("v dávce jsou všechny dotazy", len(proxy.submitted) == 4,
          str(len(proxy.submitted)))

    print("\ntělo dotazu je stejné jako u přímého volání:")
    job = proxy.submitted[0]
    check("cesta míří na /api/chat", job.get("path") == "/api/chat", str(job.get("path")))
    body = job.get("body") or {}
    expected = ollamachat.chat_payload(
        settings.ollama_fast_model, "dotaz 0",
        keep_alive=settings.ollama_keep_alive, temperature=0,
        format_schema={"type": "object"}, num_ctx=8192)
    check("tělo se shoduje do posledního pole", body == expected,
          f"{body} vs {expected}")
    check("num_ctx projde", (body.get("options") or {}).get("num_ctx") == 8192)
    check("JSON schéma projde", body.get("format") == {"type": "object"})

    print("\npriorita:")
    check("dávky jdou dozadu, ať nepředbíhají interaktivní dotazy",
          proxy.submit_payloads[0].get("priority") == settings.llm_jobs_priority,
          str(proxy.submit_payloads[0].get("priority")))

    # ── čekání na dokončení ──
    print("\nčekání na dokončení:")
    proxy = FakeProxy(pending_rounds=2)
    outs = with_proxy(proxy, lambda: llmclient.structured_json_many(
        ["a", "b"], component="test"))
    check("na čekající úlohy se počká", [o.get("i") for o in outs] == [0, 1], str(outs))
    check("stav se opravdu doptával víckrát", proxy.polls > 2, str(proxy.polls))

    # ── výpadky ──
    print("\nvýpadek proxy nesmí zabít dávkovou úlohu:")
    direct = []

    def fake_direct(prompt, **kw):
        direct.append(prompt)
        return {"primo": True}

    orig_sj = llmclient.structured_json
    llmclient.structured_json = fake_direct
    try:
        proxy = FakeProxy(behaviour="submit_fails")
        outs = with_proxy(proxy, lambda: llmclient.structured_json_many(
            ["a", "b"], component="test"))
        check("neodeslaná dávka spadne na přímé volání",
              outs == [{"primo": True}, {"primo": True}], str(outs))
        check("přímo se zavolá každý dotaz", direct == ["a", "b"], str(direct))

        direct.clear()
        proxy = FakeProxy(behaviour="poll_fails")
        outs = with_proxy(proxy, lambda: llmclient.structured_json_many(
            ["a", "b"], component="test"))
        check("výpadek při čekání taky spadne na přímé volání",
              outs == [{"primo": True}, {"primo": True}], str(outs))
    finally:
        llmclient.structured_json = orig_sj

    # ── nekompletní odpověď ──
    # Tohle je nejzákeřnější případ: kdyby se výsledky posunuly, zapsala by se
    # kategorie k JINÉ surovině a nikdo by si toho nevšiml.
    print("\nnekompletní odpověď fronty:")
    proxy = FakeProxy(behaviour="short_result")
    # Nejdřív odeslat, ať fronta opravdu VRÁTÍ o jednu úlohu míň (2 ze 3),
    # a ne prázdno – jinak by kontrola procházela z jiného důvodu.
    with_proxy(proxy, lambda: llmjobs.submit(
        [{"model": "m"}, {"model": "m"}, {"model": "m"}]))
    outs = with_proxy(proxy, lambda: llmjobs.collect("b1", count=3, wait_s=0.2))
    check("fronta vrátila o úlohu míň (2 ze 3)", len(proxy.submitted) == 3)
    check("chybí-li úloha, výsledky se NEPOSUNOU – vše je neúspěch",
          outs == [None, None, None], str(outs))

    print("\núloha skončí chybou:")
    proxy = FakeProxy(behaviour="job_error")
    with_proxy(proxy, lambda: llmjobs.submit([{"model": "m"}, {"model": "m"}]))
    outs = with_proxy(proxy, lambda: llmjobs.collect("b1", count=2, wait_s=0.2))
    check("chybná úloha je None, ostatní projdou",
          outs[0] is None and outs[1] is not None, str(outs))

    # ── zrušení ──
    print("\nzrušení dávky:")
    proxy = FakeProxy(pending_rounds=99)   # nic nedoběhne
    with_proxy(proxy, lambda: llmjobs.submit([{"model": "m"}, {"model": "m"}]))
    n = with_proxy(proxy, lambda: llmjobs.cancel("b1"))
    check("čekající úlohy se zruší", n == 2, str(n))
    check("mazalo se přes DELETE /jobs/{id}",
          all("/mgmt/v1/jobs/" in u for u in proxy.deleted), str(proxy.deleted))

    # ── vypnutá fronta ──
    print("\nvypnutá fronta:")
    settings.llm_jobs_enabled = False
    direct.clear()
    llmclient.structured_json = fake_direct
    try:
        outs = llmclient.structured_json_many(["x", "y"], component="test")
        check("bez fronty se volá přímo", direct == ["x", "y"], str(direct))
    finally:
        llmclient.structured_json = orig_sj
    check("prázdný vstup nic neposílá", llmclient.structured_json_many([]) == [])

    settings.llm_proxy_key = ""
    settings.llm_jobs_enabled = False
    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
