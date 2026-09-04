"""Testy překladu receptů: routing přes llmclient, slovníček, pojistky, počty."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_tmpdir = tempfile.mkdtemp(prefix="kucharka-translate-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmpdir}/test.db")

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Recipe  # noqa: E402
from app.modules import llmclient, translate  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


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

    # 5b) trasování LLM: dotaz i odpověď se logují pod kucharka.llm
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, r):
            self.msgs.append(r.getMessage())

    cap = _Cap()
    logging.getLogger("kucharka.llm").addHandler(cap)
    logging.getLogger("kucharka.llm").setLevel(logging.INFO)
    try:
        # skutečné structured_json (bez monkeypatche) s vypnutými providery
        # zaloguje aspoň odchozí dotaz
        import importlib
        importlib.reload(llmclient)
        from app.config import settings as _s
        old_provider = _s.llm_provider
        _s.llm_provider = "ollama"
        try:
            llmclient.structured_json("testovací prompt pro trasování", timeout=1)
        finally:
            _s.llm_provider = old_provider
        check("trasování loguje odchozí dotaz (→)",
              any(m.startswith("→") and "testovací prompt" in m for m in cap.msgs))
        check("_one_line ořezává a slepuje řádky",
              llmclient._one_line("a\nb  c" + "x" * 500).startswith("a b c")
              and len(llmclient._one_line("x" * 500)) <= 221)
    finally:
        logging.getLogger("kucharka.llm").removeHandler(cap)
        llmclient.is_available = lambda: True

    # 6) admin klíč translate_model funguje přes set_admin/as_admin
    settings.set_admin("translate_model", "  mistral-nemo:12b ")
    check("set_admin translate_model", settings.translate_model == "mistral-nemo:12b")
    check("as_admin obsahuje translate_model",
          settings.as_admin().get("translate_model") == "mistral-nemo:12b")
    settings.set_admin("translate_model", "")


EN_INSTR = "Heat the oven to 190C. Butter a tin and line the base with parchment."
CS_INSTR = "Troubu předehřejte na 190 °C. Formu vymažte máslem a vysypte moukou."


def test_status_counts():
    """Počty musí odpovídat na otázku „je vše přeloženo?" – dřív se ukazoval
    jen pool podle domény, který překladem nikdy neklesal."""
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        db.add_all([
            # český web – do překladu vůbec nespadá
            Recipe(title="Svíčková na smetaně", source_url="https://recepty.cz/1",
                   source_domain="recepty.cz", instructions=CS_INSTR),
            # cizí, nepřeložený → čeká
            Recipe(title="Angel cake", source_url="https://bbcgoodfood.com/1",
                   source_domain="bbcgoodfood.com", instructions=EN_INSTR),
            Recipe(title="Chocolate pudding", source_url="https://bbcgoodfood.com/2",
                   source_domain="bbcgoodfood.com", instructions=EN_INSTR),
            # cizí, přeložený → nečeká
            Recipe(title="Andělský dort", source_url="https://bbcgoodfood.com/3",
                   source_domain="bbcgoodfood.com", instructions=CS_INSTR,
                   original_title="Angel cake", original_instructions=EN_INSTR),
            # cizí, přeložený titul, ale postup zůstal anglicky → částečný
            Recipe(title="Vánoční pudink", source_url="https://bbcgoodfood.com/4",
                   source_domain="bbcgoodfood.com", instructions=EN_INSTR,
                   original_title="Christmas pudding", original_instructions=EN_INSTR),
            # cizí bez postupu – prázdný postup se nehodnotí
            Recipe(title="Tiramisu", source_url="https://bbcgoodfood.com/5",
                   source_domain="bbcgoodfood.com", instructions=None,
                   original_title="Tiramisu"),
        ])
        db.commit()
    finally:
        db.close()

    st = translate.status()
    check("recipes_total počítá vše", st["recipes_total"] == 6, str(st["recipes_total"]))
    check("foreign_total = jen cizí domény (pool)", st["foreign_total"] == 5,
          str(st["foreign_total"]))
    check("foreign_pending = jen skutečně nepřeložené", st["foreign_pending"] == 2,
          str(st["foreign_pending"]))
    check("foreign_partial = přeložený titul, nepřeložený postup",
          st["foreign_partial"] == 1, str(st["foreign_partial"]))

    # fronta automatického překladu = přesně ty čekající (a ne celý korpus)
    db = SessionLocal()
    try:
        rows = translate._foreign_probe_rows(db)
        ids = [r.id for r in rows if translate._is_untranslated(r)]
        check("fronta překladu = přesně počítadlo „čeká na překlad\"",
              len(ids) == st["foreign_pending"] == 2, str(len(ids)))
        check("recept s uloženým originálem se do fronty nevrací",
              all(r.original_title is None for r in rows if translate._is_untranslated(r)))
        # kandidáti na dohledání originálu: cizí, vypadá česky, bez originálu
        check("kandidáti na originál: žádný (vše má originál nebo je cizojazyčné)",
              translate._reset_candidate_ids(db) == [],
              str(translate._reset_candidate_ids(db)))
    finally:
        db.close()

    # po „přeložení" zbývajících dvou musí pending klesnout na nulu
    db = SessionLocal()
    try:
        for r in db.query(Recipe).filter(Recipe.instructions == EN_INSTR).all():
            if r.original_title is None:
                r.original_title = r.title
            r.title = "Přeloženo " + r.title
            r.instructions = CS_INSTR
        db.commit()
    finally:
        db.close()
    st = translate.status()
    check("po překladu je pending 0 (= „vše přeloženo\")", st["foreign_pending"] == 0,
          str(st["foreign_pending"]))
    check("po překladu je partial 0", st["foreign_partial"] == 0, str(st["foreign_partial"]))
    check("pool podle domény zůstává (nemá klesat)", st["foreign_total"] == 5,
          str(st["foreign_total"]))


def main():
    orig_avail = llmclient.is_available
    orig_sj = llmclient.structured_json
    try:
        test_translate_fields([])
        llmclient.is_available = orig_avail
        llmclient.structured_json = orig_sj
        test_status_counts()
    finally:
        llmclient.is_available = orig_avail
        llmclient.structured_json = orig_sj
    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
