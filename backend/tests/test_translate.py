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

    # 2) úspěšný překlad se mapuje 1:1
    llmclient.is_available = lambda: True

    def fake_structured(prompt, **kw):
        calls.append({"prompt": prompt, "kw": kw})
        return {
            "title": "Pečený květák",
            "ingredients": ["1 květák", "2 lžíce sezamových semínek"],
            "instructions": "Pečte 25 minut.",
        }

    llmclient.structured_json = fake_structured
    out = translate._translate_fields(
        "Roast cauliflower", ["1 cauliflower", "2 tbsp sesame seeds"], "Roast for 25 min."
    )
    check("překlad vrací titul", out and out["title"] == "Pečený květák")
    check("překlad drží počet ingrediencí", out and len(out["ingredients"]) == 2)
    p = calls[-1]["prompt"]
    check("prompt obsahuje slovníček (květák)", "cauliflower=květák" in p)
    check("prompt obsahuje slovníček (sezam)", "sezamová semínka" in p)
    check("prompt zakazuje novotvary", "novotvary" in p)
    check("volání má JSON schéma", calls[-1]["kw"].get("schema") == translate._SCHEMA)
    check(
        "timeout jde z llm_match_timeout_s",
        calls[-1]["kw"].get("timeout", 0) >= 120,
    )

    # 3) nesedící počet ingrediencí → originál se zachová (None)
    def fake_short(prompt, **kw):
        return {"title": "X", "ingredients": ["jen jedna"], "instructions": ""}

    llmclient.structured_json = fake_short
    out = translate._translate_fields("T", ["a", "b"], "i")
    check("nesedící počet ingrediencí → None", out is None)

    # 4) chyba volání → None, nespadne
    def fake_none(prompt, **kw):
        return None

    llmclient.structured_json = fake_none
    out = translate._translate_fields("T", ["a"], "i")
    check("None z LLM → None", out is None)

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
