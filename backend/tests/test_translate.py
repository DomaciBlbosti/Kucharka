"""Testy překladu receptů: routing přes llmclient, slovníček, pojistky."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.modules import llmclient, translate  # noqa: E402

PASSED = FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}")


def test_translate_fields(monkeypatch_calls):
    calls = monkeypatch_calls

    # 1) nedostupný provider → None, žádné volání
    llmclient.is_available = lambda: False
    out = translate._translate_fields("Roast cauliflower", ["1 cauliflower"], "Roast it.")
    check("nedostupný provider vrací None", out is None)
    check("nedostupný provider nevolá LLM", not calls)

    # 2) úspěšný překlad se mapuje 1:1 (dvě malá volání: meta + postup)
    llmclient.is_available = lambda: True

    def fake_structured(prompt, **kw):
        calls.append({"prompt": prompt, "kw": kw})
        if "ingredients" in (kw.get("schema") or {}).get("properties", {}):
            return {"title": "Pečený květák",
                    "ingredients": ["1 květák", "2 lžíce sezamových semínek"]}
        return {"instructions": "Pečte 25 minut."}

    llmclient.structured_json = fake_structured
    out = translate._translate_fields(
        "Roast cauliflower", ["1 cauliflower", "2 tbsp sesame seeds"], "Roast for 25 min."
    )
    check("překlad vrací titul", out and out["title"] == "Pečený květák")
    check("překlad drží počet ingrediencí", out and len(out["ingredients"]) == 2)
    check("překlad vrací postup", out and out["instructions"] == "Pečte 25 minut.")
    check("dvě volání (meta + postup)", len(calls) == 2)
    meta_p, instr_p = calls[0]["prompt"], calls[1]["prompt"]
    check("meta prompt obsahuje slovníček (květák)", "cauliflower=květák" in meta_p)
    check("postup prompt obsahuje slovníček", "cauliflower=květák" in instr_p)
    check("prompt zakazuje novotvary", "novotvary" in meta_p)
    check("meta volání má malé num_ctx (vejde se na GPU)",
          calls[0]["kw"].get("num_ctx", 99999) <= 4096)
    check("postup volání má malé num_ctx",
          calls[1]["kw"].get("num_ctx", 99999) <= 4096)
    check("timeout jde z llm_match_timeout_s",
          calls[0]["kw"].get("timeout", 0) >= 120)

    # 3) nesedící počet ingrediencí → originál se zachová (None)
    def fake_short(prompt, **kw):
        return {"title": "X", "ingredients": ["jen jedna"], "instructions": ""}

    llmclient.structured_json = fake_short
    out = translate._translate_fields("T", ["a", "b"], "i")
    check("nesedící počet ingrediencí → None", out is None)

    # 4) chyba prvního volání → None, nespadne
    def fake_none(prompt, **kw):
        return None

    llmclient.structured_json = fake_none
    out = translate._translate_fields("T", ["a"], "i")
    check("None z LLM → None", out is None)

    # 4b) meta projde, postup vyhoří → částečný výsledek (instructions="")
    def fake_partial(prompt, **kw):
        if "ingredients" in (kw.get("schema") or {}).get("properties", {}):
            return {"title": "Guláš", "ingredients": ["1 cibule"]}
        return None  # timeout postupu

    llmclient.structured_json = fake_partial
    out = translate._translate_fields("Goulash", ["1 onion"], "Cook it long.")
    check("vyhořelý postup → titul+ingredience se uloží",
          out is not None and out["title"] == "Guláš" and out["ingredients"] == ["1 cibule"])
    check("vyhořelý postup → instructions prázdné (volající nechá originál)",
          out is not None and out["instructions"] == "")

    # 4c) prázdný postup → druhé volání se vůbec nedělá
    probe = []

    def fake_count(prompt, **kw):
        probe.append(1)
        return {"title": "T", "ingredients": ["a"]}

    llmclient.structured_json = fake_count
    out = translate._translate_fields("T", ["a"], "")
    check("prázdný postup → jen jedno volání", len(probe) == 1 and out is not None)

    # 5) samostatný model pro překlad se propaguje do volání
    from app.config import settings

    def fake_capture(prompt, **kw):
        calls.append({"prompt": prompt, "kw": kw})
        return {"title": "T", "ingredients": ["a"], "instructions": ""}

    llmclient.structured_json = fake_capture
    old = settings.translate_model
    try:
        settings.translate_model = "aya-expanse:8b"
        translate._translate_fields("T", ["a"], "i")
        check(
            "translate_model → ollama_model ve volání",
            calls[-1]["kw"].get("ollama_model") == "aya-expanse:8b",
        )
        settings.translate_model = ""
        translate._translate_fields("T", ["a"], "i")
        check(
            "prázdný translate_model → ollama_model None (rychlý model)",
            calls[-1]["kw"].get("ollama_model") is None,
        )
    finally:
        settings.translate_model = old

    # 6) admin klíč translate_model funguje přes set_admin/as_admin
    settings.set_admin("translate_model", "  mistral-nemo:12b ")
    check("set_admin translate_model", settings.translate_model == "mistral-nemo:12b")
    check("as_admin obsahuje translate_model",
          settings.as_admin().get("translate_model") == "mistral-nemo:12b")
    settings.set_admin("translate_model", "")


def main():
    orig_avail = llmclient.is_available
    orig_sj = llmclient.structured_json
    try:
        test_translate_fields([])
    finally:
        llmclient.is_available = orig_avail
        llmclient.structured_json = orig_sj
    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
