"""Audit korpusu receptů – READ-ONLY profil + stratifikovaný vzorek.

Cíl: dva exporty, ze kterých se rozhodne, jak (a jestli vůbec) korpus třídit:
  - analysis/corpus_profile.json  – agregáty nad celým korpusem (Export A),
  - analysis/corpus_sample.jsonl  – ~600–700 receptů k ručnímu labelování (B).

Nic neklasifikuje, nic nemaže, do DB nezapisuje ani bajt. Metriky se počítají
čistě v Pythonu nad dávkami (keyset pagination po id, konstantní paměť);
suroviny se k dávce tahají jedním dotazem, ne N+1.

Mapování na skutečné schéma (viz models.py):
  - postup      = recipe.instructions (jeden Text sloupec, ne kroky v tabulce)
  - zdrojová URL = recipe.source_url, doména = recipe.source_domain (plní se
    při ingestu; recepty z fotky/AI – photo:// a ai:// – ji nemají a z
    doménového rozpadu se vypouštějí, nedopočítává se)
  - množství/jednotka = recipe_ingredient.raw_text (řádek, jak ho vidí
    uživatel; amount/unit jsou z něj parsované)

Spuštění z CLI (z adresáře backend/):
    python -m app.modules.corpus_audit --profile
    python -m app.modules.corpus_audit --sample --seed 42
    python -m app.modules.corpus_audit            # obojí najednou (1 průchod)
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import traceback
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Ingredient, Recipe, RecipeIngredient

log = logging.getLogger("kucharka.corpus_audit")

# Výstupy do analysis/ v kořeni projektu; stahují se přes admin endpointy.
ANALYSIS_DIR = Path(__file__).resolve().parents[3] / "analysis"
PROFILE_PATH = ANALYSIS_DIR / "corpus_profile.json"
SAMPLE_PATH = ANALYSIS_DIR / "corpus_sample.jsonl"

_BATCH = 500          # keyset dávka receptů
_SAMPLE_TRUNC = 600   # ořez postupu ve vzorku (na hranici slova)


# ─── Normalizace a kmeny sloves ──────────────────────────────────────────────

def norm(s: str) -> str:
    """Lowercase + odstranění diakritiky (čeština je flektivní, hledáme kmeny)."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Kmeny sloves (po normalizaci) ve DVOU třídách. Vzorek nese matched_stems
# i matched_prep_stems, ať se false positives odhalí očima.
#
# COOK = tepelná/kuchařská úprava. Úpravy proti startovní sadě ze zadání:
#   - "sek" dostal (?!und) – "30 sekund" je v postupech všudypřítomné,
#   - doplněné PŘEDPONOVÉ tvary: match na hranici slova nechytal "smícháme",
#     "vyšleháme", "orestujeme", "nastrouháme", "rozmixujte", "svařte"… a
#     koktejly/saláty/nepečené dezerty pak falešně vycházely jako "0 sloves"
#     (ověřeno nad produkčním vzorkem – vrstva no_cook_verbs byla z většiny
#     falešný poplach). Generická volitelná předpona (s|o|roz|…) by byla
#     kratší, ale chytala by "SPECiální" (s+pec) nebo "OVAR" (o+var), proto
#     se tvary vyjmenovávají explicitně, stejně jako v původní sadě.
# Známé zbytkové false positives (ponechané kvůli recall, viz vzorek):
# "var"→"varianta/varná", "pec"→"pečivo", "mix"→"mixér" (nástroj ≈ vaření).
COOK_STEMS = [
    "var", "uvar", "svar", "povar", "provar", "prevar", "zavar",
    "pec", "pek", "zapec", "opec", "upec", "propec",
    "smaz", "osmaz", "dus", "podus", "zadus", "restu", "orest",
    "ohr", "zahr", "prohr",
    "mich", "vmich", "zamich", "smich", "promich", "rozmich", "umich",
    "sleh", "vysleh", "usleh", "mix", "rozmix",
    "kraj", "nakraj", "rozkraj", "strouh", "nastrouh",
    "sek", "nasek", "posek", "usek", "rozsek",
    "oloup", "rozmack", "vymack", "vymaz", "vysyp", "nasyp",
    "hnet", "zadel", "marin", "gril", "blansir",
    "ced", "scedit", "proced", "roztop", "rozehr", "rozpust", "ochut", "osol",
    "opepr", "obal", "zapras", "prosej",
    "nalij", "podlij", "zalij", "prelij", "dolij", "polij",
]

# PREP = úkony bez tepelné úpravy. Bez nich vycházely jako podezřelé i
# recepty, které jsou v pořádku – pomazánky, nálevy, drinky, dekorace na
# dorty, studená kuchyně. Nad produkčním vzorkem měl 81 ze 98 receptů
# "0 vařicích sloves" popsané úkony právě těmito slovesy.
PREP_STEMS = [
    "prid", "potr", "nech", "podav", "serv",
    "napln", "plni", "poklad", "poloz", "vloz", "vkla", "vyklop",
    "ozdob", "zdob", "posyp", "natr", "utr", "namaz", "pomaz",
    "vychlad", "zamraz", "zmraz", "susi",
    "namoc", "namac", "omy", "osus", "oplach", "protres",
    "rozval", "vyval", "stoc", "zabal", "tvarov", "vytvar", "tvor",
    "rozdel", "vykroj", "vyrez", "odstran", "obrat", "otoc",
    "zhust", "zredi", "vysklad", "posklad", "slep", "dopln",
    "urovn", "navrs", "udel",
]

# Negativní lookahead u kmenů, které by jinak spolkly běžná nesouvisející
# slova. Ověřeno nad produkčním vzorkem, ne odhadem.
_STEM_GUARDS = {
    "sek": "und",      # „30 sekund"
    "potr": "eb",      # „potřeba / potřebovat"
    "zahr": "n",       # „zahrneme"
    "zavar": "enin",   # „zavařenina"
    "pomaz": "ank",    # „pomazánka" (podstatné jméno, ne úkon)
}


def _alternation(stems: list[str]) -> re.Pattern:
    """Jedna kompilovaná alternace: delší kmeny první (ať "zapec" nespolkne
    "pec" jinde než na své pozici), match jen na hranici slova."""
    body = "|".join(
        s + (f"(?!{_STEM_GUARDS[s]})" if s in _STEM_GUARDS else "")
        for s in sorted(stems, key=len, reverse=True)
    )
    return re.compile(rf"\b(?:{body})")


_STEM_RE = _alternation(COOK_STEMS)
_PREP_RE = _alternation(PREP_STEMS)

_TIME_RE = re.compile(r"\d+\s*(min|minut|hod|h\b)")
_TEMP_RE = re.compile(r"\d+\s*(°|st\.|stupn)")
_NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]", re.M)
_WORD_RE = re.compile(r"[a-z]{2,}")


def matched_stems(norm_instr: str) -> list[str]:
    """Seznam RŮZNÝCH vařicích kmenů, které se v (norm.) postupu trefily."""
    return sorted(set(_STEM_RE.findall(norm_instr)))


def matched_prep_stems(norm_instr: str) -> list[str]:
    """Totéž pro úkony bez tepelné úpravy (přidat, potřít, ozdobit…)."""
    return sorted(set(_PREP_RE.findall(norm_instr)))


def _n_steps(instructions: str) -> int:
    """Počet kroků: max z odstavců (\\n\\n), řádků a číslování '1.' / '1)'."""
    t = (instructions or "").strip()
    if not t:
        return 0
    paras = sum(1 for p in re.split(r"\n\s*\n", t) if p.strip())
    lines = sum(1 for ln in t.splitlines() if ln.strip())
    numbered = len(_NUMBERED_STEP_RE.findall(t))
    return max(paras, lines, numbered, 1)


def _coverage(norm_instr: str, ingredient_texts: list[str]) -> float:
    """Podíl surovin, jejichž první „pořádné" slovo se vyskytne v postupu.

    První pořádné slovo = první alfabetický token ≥4 znaky (přeskočí „200",
    „g", „ks"…). Kvůli flektivnosti se hledá PREFIX slova (poslední ≤2 znaky
    se uříznou, min. 4 znaky): „mouky" → „mouk" najde i „mouku"/„mouka".
    Suroviny bez takového slova se nepočítají ani do jmenovatele.
    """
    hits = eligible = 0
    for raw in ingredient_texts:
        word = next((w for w in _WORD_RE.findall(norm(raw)) if len(w) >= 4), None)
        if word is None:
            continue
        eligible += 1
        probe = word[: max(4, len(word) - 2)]
        if probe in norm_instr:
            hits += 1
    return round(hits / eligible, 3) if eligible else 0.0


def recipe_metrics(title: str, instructions: str | None, ingredient_texts: list[str]) -> dict:
    """Všechny odvozené metriky jednoho receptu (bez LLM, bez zápisu)."""
    instr = (instructions or "").strip()
    ni = norm(instr)
    stems = matched_stems(ni)
    prep = matched_prep_stems(ni)
    return {
        "n_ingredients": len(ingredient_texts),
        "n_steps": _n_steps(instr),
        "instr_chars": len(instr),
        "n_cook_verbs": len(stems),
        "matched_stems": stems,
        "n_prep_verbs": len(prep),
        "matched_prep_stems": prep,
        # Podezřelý je až recept BEZ jakékoli akce – ani vaření, ani úkon.
        # Samotné "0 vařicích sloves" je u studené kuchyně normální stav.
        "has_no_action": not stems and not prep,
        "has_time": bool(_TIME_RE.search(ni)),
        "has_temp": bool(_TEMP_RE.search(ni)),
        "ingr_coverage": _coverage(ni, ingredient_texts),
        "title_chars": len((title or "").strip()),
        "has_empty_instr": len(instr) < 20,
        "has_empty_ingr": len(ingredient_texts) == 0,
    }


# ─── Buckety (hranice přesně dle zadání – globál i domény jsou porovnatelné) ─

def _bucket(value: float, edges: list[tuple[str, float, float]]) -> str:
    for name, lo, hi in edges:
        if lo <= value <= hi:
            return name
    return edges[-1][0]


_ING_EDGES = [("0", 0, 0), ("1-2", 1, 2), ("3", 3, 3), ("4-5", 4, 5),
              ("6-10", 6, 10), ("11-20", 11, 20), ("21+", 21, float("inf"))]
_STEP_EDGES = [("0", 0, 0), ("1", 1, 1), ("2-3", 2, 3), ("4-6", 4, 6),
               ("7-12", 7, 12), ("13+", 13, float("inf"))]
_CHAR_EDGES = [("0-49", 0, 49), ("50-199", 50, 199), ("200-499", 200, 499),
               ("500-1499", 500, 1499), ("1500+", 1500, float("inf"))]
_VERB_EDGES = [("0", 0, 0), ("1", 1, 1), ("2-3", 2, 3), ("4-6", 4, 6),
               ("7+", 7, float("inf"))]
_COV_EDGES = [("0.0-0.2", 0.0, 0.2), ("0.2-0.5", 0.2001, 0.5),
              ("0.5-0.8", 0.5001, 0.8), ("0.8-1.0", 0.8001, 1.0)]


def _empty_hist(edges) -> dict:
    return {name: 0 for name, _lo, _hi in edges}


def _median_from_counter(c: Counter) -> float:
    n = sum(c.values())
    if not n:
        return 0
    mid = (n + 1) // 2
    seen = 0
    for value in sorted(c):
        seen += c[value]
        if seen >= mid:
            return value
    return 0


# ─── Stav běhu (stejný vzor jako ostatní background joby) ────────────────────

_lock = threading.Lock()
_state: dict = {
    "running": False, "phase": None, "done": 0, "total": 0,
    "error": None, "finished_at": None, "duration_s": None, "seed": None,
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        s = dict(_state)
    for key, path in (("profile", PROFILE_PATH), ("sample", SAMPLE_PATH)):
        s[f"{key}_exists"] = path.exists()
        s[f"{key}_bytes"] = path.stat().st_size if path.exists() else 0
        s[f"{key}_mtime"] = (
            datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
            if path.exists() else None
        )
    return s


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


# ─── Hlavní běh ──────────────────────────────────────────────────────────────

def run(seed: int = 42, do_profile: bool = True, do_sample: bool = True) -> dict:
    """Jeden průchod korpusem: profil (agregáty) + kompaktní metriky pro
    stratifikaci; vybraný vzorek se pak dotáhne druhým, malým průchodem.
    Read-only: session jen čte, žádný commit."""
    started = time.monotonic()
    db = SessionLocal()
    try:
        total_recipes = db.scalar(select(func.count(Recipe.id))) or 0
        total_ingredients = db.scalar(select(func.count(Ingredient.id))) or 0
        _set(phase="scan", done=0, total=total_recipes, seed=seed)

        g = {
            "n_ingredients": _empty_hist(_ING_EDGES),
            "n_steps": _empty_hist(_STEP_EDGES),
            "instr_chars": _empty_hist(_CHAR_EDGES),
            "n_cook_verbs": _empty_hist(_VERB_EDGES),
            "n_prep_verbs": _empty_hist(_VERB_EDGES),
            "ingr_coverage": _empty_hist(_COV_EDGES),
            "has_time": 0, "has_temp": 0,
            "has_empty_instr": 0, "has_empty_ingr": 0,
            "has_no_action": 0,
        }
        # per-doména: countery hodnot (kvůli mediánům) + podílové čitatele
        dom: dict[str, dict] = {}
        title_counter: Counter = Counter()
        # kompaktní tuply pro stratifikaci: (id, n_ing, instr_chars, n_verbs, domain)
        compact: list[tuple] = []

        last_id = 0
        done = 0
        while True:
            recipes = db.execute(
                select(Recipe.id, Recipe.title, Recipe.instructions, Recipe.source_domain)
                .where(Recipe.id > last_id)
                .order_by(Recipe.id)
                .limit(_BATCH)
            ).all()
            if not recipes:
                break
            ids = [r.id for r in recipes]
            last_id = ids[-1]
            ing_rows = db.execute(
                select(RecipeIngredient.recipe_id, RecipeIngredient.raw_text)
                .where(RecipeIngredient.recipe_id.in_(ids))
                .order_by(RecipeIngredient.id)
            ).all()
            by_recipe: dict[int, list[str]] = {}
            for rid, raw in ing_rows:
                by_recipe.setdefault(rid, []).append(raw or "")

            for rec in recipes:
                m = recipe_metrics(rec.title, rec.instructions, by_recipe.get(rec.id, []))
                g["n_ingredients"][_bucket(m["n_ingredients"], _ING_EDGES)] += 1
                g["n_steps"][_bucket(m["n_steps"], _STEP_EDGES)] += 1
                g["instr_chars"][_bucket(m["instr_chars"], _CHAR_EDGES)] += 1
                g["n_cook_verbs"][_bucket(m["n_cook_verbs"], _VERB_EDGES)] += 1
                g["n_prep_verbs"][_bucket(m["n_prep_verbs"], _VERB_EDGES)] += 1
                g["ingr_coverage"][_bucket(m["ingr_coverage"], _COV_EDGES)] += 1
                for flag in ("has_time", "has_temp", "has_empty_instr",
                             "has_empty_ingr", "has_no_action"):
                    g[flag] += int(m[flag])

                domain = (rec.source_domain or "").replace("www.", "") or None
                if domain is not None:
                    d = dom.setdefault(domain, {
                        "count": 0, "ing": Counter(), "chars": Counter(),
                        "verbs": Counter(), "zero_verbs": 0, "no_action": 0,
                        "empty_instr": 0, "few_ing": 0,
                    })
                    d["count"] += 1
                    d["ing"][m["n_ingredients"]] += 1
                    d["chars"][m["instr_chars"]] += 1
                    d["verbs"][m["n_cook_verbs"]] += 1
                    d["zero_verbs"] += int(m["n_cook_verbs"] == 0)
                    d["no_action"] += int(m["has_no_action"])
                    d["empty_instr"] += int(m["has_empty_instr"])
                    d["few_ing"] += int(m["n_ingredients"] <= 3)

                title_counter[norm(rec.title).strip()] += 1
                compact.append((
                    rec.id, m["n_ingredients"], m["instr_chars"],
                    m["n_cook_verbs"], domain, m["has_no_action"],
                ))
                done += 1
            _set(done=done)

        result: dict = {"total_recipes": total_recipes}

        if do_profile:
            _set(phase="profile")
            _write_profile(g, dom, title_counter, total_recipes, total_ingredients)
            result["profile_path"] = str(PROFILE_PATH)

        if do_sample:
            _set(phase="sample")
            sample_ids = _select_sample(compact, dom, seed)
            _write_sample(db, sample_ids, seed)
            result["sample_rows"] = len(sample_ids)
            result["sample_path"] = str(SAMPLE_PATH)

        duration = round(time.monotonic() - started, 1)
        _set(error=None, duration_s=duration)
        result["duration_s"] = duration
        log.info("audit korpusu hotový za %s s (%s receptů)", duration, total_recipes)
        return result
    finally:
        db.close()


def _write_profile(g, dom, title_counter, total_recipes, total_ingredients) -> None:
    top = sorted(dom.items(), key=lambda kv: -kv[1]["count"])
    rows = []
    other = {"count": 0, "ing": Counter(), "chars": Counter(), "verbs": Counter(),
             "zero_verbs": 0, "no_action": 0, "empty_instr": 0, "few_ing": 0}
    for i, (domain, d) in enumerate(top):
        if i < 30:
            rows.append((domain, d))
        else:
            other["count"] += d["count"]
            for key in ("ing", "chars", "verbs"):
                other[key] += d[key]
            for key in ("zero_verbs", "no_action", "empty_instr", "few_ing"):
                other[key] += d[key]
    if other["count"]:
        rows.append(("__other__", other))

    def _pct(part, whole):
        return round(100.0 * part / whole, 1) if whole else 0.0

    by_domain = [
        {
            "domain": domain,
            "count": d["count"],
            "median_n_ingredients": _median_from_counter(d["ing"]),
            "median_instr_chars": _median_from_counter(d["chars"]),
            "median_n_cook_verbs": _median_from_counter(d["verbs"]),
            "pct_zero_cook_verbs": _pct(d["zero_verbs"], d["count"]),
            "pct_no_action": _pct(d["no_action"], d["count"]),
            "pct_empty_instr": _pct(d["empty_instr"], d["count"]),
            "pct_few_ingredients": _pct(d["few_ing"], d["count"]),
        }
        for domain, d in rows
    ]

    dup_counts = [c for c in title_counter.values() if c >= 2]
    profile = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_recipes": total_recipes,
        "total_ingredients": total_ingredients,
        "global": g,
        "by_domain": by_domain,
        "duplicate_titles": {
            "distinct_titles": len(title_counter),
            "titles_with_2plus": len(dup_counts),
            "max_cluster_size": max(dup_counts, default=1),
            "top_20": [
                {"title": t, "count": c}
                for t, c in title_counter.most_common(20) if c >= 2
            ],
        },
    }
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _select_sample(compact: list[tuple], dom: dict, seed: int) -> dict[int, list[str]]:
    """Vrátí {recipe_id: [strata…]} – deterministicky pro daný seed a DB.

    compact je v pořadí id ASC (keyset scan), takže rng.sample nad stejnými
    daty dá stejný výsledek."""
    rng = random.Random(seed)
    chosen: dict[int, list[str]] = {}

    def take(name: str, pool: list[int], k: int):
        for rid in rng.sample(pool, min(k, len(pool))):
            chosen.setdefault(rid, []).append(name)

    take("random", [t[0] for t in compact], 200)
    take("few_ingredients", [t[0] for t in compact if t[1] <= 3], 100)
    take("short_instr", [t[0] for t in compact if t[2] < 300], 100)
    # Dvě vrstvy místo jedné: „no_action" je ta skutečně podezřelá (žádná
    # akce v postupu), „no_cook_verbs" zůstává menší kvůli porovnatelnosti
    # se staršími exporty – je z většiny studená kuchyně, ne vada.
    take("no_action", [t[0] for t in compact if t[5]], 100)
    take("no_cook_verbs", [t[0] for t in compact if t[3] == 0], 50)
    top10 = [d for d, _ in sorted(dom.items(), key=lambda kv: -kv[1]["count"])[:10]]
    for domain in top10:
        take(f"domain:{domain}", [t[0] for t in compact if t[4] == domain], 20)
    return chosen


def _truncate_words(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit // 2 else cut).rstrip(), True


def _write_sample(db, chosen: dict[int, list[str]], seed: int) -> None:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    ids = sorted(chosen)
    with SAMPLE_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "_header": True, "seed": seed, "count": len(ids),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False) + "\n")
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            recipes = db.execute(
                select(Recipe.id, Recipe.title, Recipe.instructions,
                       Recipe.source_url, Recipe.source_domain)
                .where(Recipe.id.in_(batch))
                .order_by(Recipe.id)
            ).all()
            ing_rows = db.execute(
                select(RecipeIngredient.recipe_id, RecipeIngredient.raw_text)
                .where(RecipeIngredient.recipe_id.in_(batch))
                .order_by(RecipeIngredient.id)
            ).all()
            by_recipe: dict[int, list[str]] = {}
            for rid, raw in ing_rows:
                by_recipe.setdefault(rid, []).append(raw or "")
            for rec in recipes:
                texts = by_recipe.get(rec.id, [])
                m = recipe_metrics(rec.title, rec.instructions, texts)
                instr, truncated = _truncate_words(
                    (rec.instructions or "").strip(), _SAMPLE_TRUNC
                )
                fh.write(json.dumps({
                    "id": rec.id,
                    "strata": sorted(chosen[rec.id]),
                    "domain": (rec.source_domain or "").replace("www.", "") or None,
                    "url": rec.source_url,
                    "title": rec.title,
                    "ingredients": texts,
                    "instructions": instr,
                    "instr_truncated": truncated,
                    "metrics": {
                        k: m[k] for k in (
                            "n_ingredients", "n_steps", "instr_chars",
                            "n_cook_verbs", "matched_stems",
                            "n_prep_verbs", "matched_prep_stems",
                            "has_no_action", "has_time",
                            "has_temp", "ingr_coverage", "title_chars",
                        )
                    },
                }, ensure_ascii=False) + "\n")


# ─── Async wrapper pro admin ─────────────────────────────────────────────────

def run_async(seed: int = 42) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, phase="start", done=0, total=0,
                      error=None, finished_at=None)

    def _worker():
        try:
            run(seed=seed)
        except Exception as exc:  # noqa: BLE001 - vlákno nesmí umřít potichu
            log.error("audit korpusu selhal: %s\n%s", exc, traceback.format_exc())
            _set(error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _set(running=False, phase=None, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="corpus-audit").start()
    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Read-only audit korpusu receptů")
    ap.add_argument("--profile", action="store_true", help="jen profil")
    ap.add_argument("--sample", action="store_true", help="jen vzorek")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    both = not args.profile and not args.sample
    out = run(
        seed=args.seed,
        do_profile=args.profile or both,
        do_sample=args.sample or both,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
