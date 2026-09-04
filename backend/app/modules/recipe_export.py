"""Čitelný export receptů pro ruční kontrolu kvality zpracování.

K čemu to je: podívat se na hotové recepty vedle sebe s tím, co přišlo ze
zdroje, a posoudit očima, jestli je zpracování v pořádku. Profil z
`corpus_audit` říká, KOLIK receptů je podezřelých; tohle ukazuje PROČ.

Text receptu prochází třemi vrstvami a export je ukazuje všechny:

  1. `raw_json`            – co odešlo ze scraperu při ingestu (nejsyrovější)
  2. `original_*`          – text před strojovým překladem (jen u přeložených)
  3. `title`/`instructions`– co uživatel vidí v appce

U surovin totéž: `raw_text` (řádek, jak ho vidí uživatel), případně
`original_raw_text` (před překladem) a napravo výsledek párování – na kterou
surovinu ze slovníku to sedlo, kolik z toho vyšlo gramů a kalorií.

Dva výstupy z jednoho průchodu:
  - `analysis/recipe_export.html` – na čtení, otevře se v prohlížeči,
    filtry a přepínače jsou v souboru (žádné CDN, funguje offline),
  - `analysis/recipe_export.xml`  – tatáž data strojově.

Export je READ-ONLY, do databáze nezapisuje ani bajt.

Spuštění z CLI (z adresáře backend/):
    python -m app.modules.recipe_export --limit 100
    python -m app.modules.recipe_export --pick translated --limit 50
    python -m app.modules.recipe_export --pick unmatched --domain recepty.cz
"""
from __future__ import annotations

import html
import json
import logging
import random
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from ..models import Recipe, RecipeIngredient
from . import corpus_audit

log = logging.getLogger("kucharka.recipe_export")

ANALYSIS_DIR = corpus_audit.ANALYSIS_DIR
HTML_PATH = ANALYSIS_DIR / "recipe_export.html"
XML_PATH = ANALYSIS_DIR / "recipe_export.xml"

# Ořez `raw_json` v exportu. Celý bývá i desítky kB (nese HTML útržky) a
# stovka receptů by z výstupu udělala soubor, který prohlížeč nepobere.
_RAW_TRUNC = 4000

# Strop počtu receptů v jednom exportu. NENÍ to opatrnost, je to nutnost:
# naměřeno ~9 kB HTML a ~70 MB paměti na tisíc receptů, takže celý korpus
# (171 tisíc) by znamenal 1,6GB soubor a přes 10 GB RAM – appka na NASu by
# skončila na OOM. A i kdyby doběhla, takové HTML žádný prohlížeč neotevře.
# Export je na ČTENÍ; na agregáty nad celým korpusem je corpus_audit.
# Strop se vynucuje tady, ne až v API, aby ho neobešel běh z CLI.
MAX_LIMIT = 2000

# Výběrové režimy. Pointa je dostat na oči TY recepty, u kterých se dá čekat
# problém, ne průřez průměrem.
PICKS = {
    "random":     "náhodný vzorek (reprodukovatelný podle seedu)",
    "translated": "jen strojově přeložené – tam je vidět kvalita překladu",
    "unmatched":  "jen ty s nenapárovanou surovinou",
    "no_instr":   "jen ty s prázdným nebo skoro prázdným postupem",
    "newest":     "nejnovější",
}


# ─── Výběr receptů ───────────────────────────────────────────────────────────

def pick_ids(db, pick: str, limit: int | None, seed: int,
             domain: str | None) -> list[int]:
    """Vrať id receptů k exportu, vzestupně podle id.

    Vybírá se v SQL (ne načtením korpusu do paměti). Náhodný vzorek jde přes
    seedovaný výběr nad seznamem id – `ORDER BY RAND()` by na 171 tisících
    řádcích tabulku třídil celou.

    `limit=None` znamená „všechno, co výběru odpovídá" – tak to potřebuje
    kontrola v appce (`modules/review`), která si stránkuje sama a nesmí
    dostat jen výřez.
    """
    q = select(Recipe.id)
    if domain:
        q = q.where(Recipe.source_domain == domain)

    if pick == "translated":
        q = q.where(Recipe.original_instructions.is_not(None))
    elif pick == "unmatched":
        q = q.where(Recipe.id.in_(
            select(RecipeIngredient.recipe_id)
            .where(RecipeIngredient.ingredient_id.is_(None))
            .where(RecipeIngredient.nonfood.is_(False))
        ))
    elif pick == "no_instr":
        # Délka se počítá v databázi – stáhnout postupy všech receptů kvůli
        # `len()` by byly stovky MB (viz stejná past ve feed.recompute_all).
        q = q.where(
            func.coalesce(func.length(func.trim(Recipe.instructions)), 0) < 120
        )

    if pick == "newest":
        q = q.order_by(Recipe.id.desc())
        return sorted(db.scalars(q if limit is None else q.limit(limit)))

    ids = list(db.scalars(q))
    if limit is None or len(ids) <= limit:
        return sorted(ids)
    return sorted(random.Random(seed).sample(ids, limit))


# ─── Sestavení dat jednoho receptu ───────────────────────────────────────────

def _ingredient_rows(rec: Recipe) -> list[dict]:
    # Podle id, tedy v pořadí, v jakém řádky přišly z receptu. Vztah
    # `Recipe.ingredients` žádné řazení nemá, takže bez tohohle je pořadí
    # náhodné – při čtení vedle sebe s postupem je to k nepoužití.
    out = []
    for ri in sorted(rec.ingredients, key=lambda x: x.id or 0):
        out.append({
            "raw_text": ri.raw_text or "",
            "original_raw_text": ri.original_raw_text or "",
            "matched": ri.ingredient.name_cs if ri.ingredient else "",
            "matched_id": ri.ingredient_id,
            "matched_category": (ri.ingredient.category_path
                                 or ri.ingredient.category or "") if ri.ingredient else "",
            "amount": ri.amount,
            "unit": ri.unit or "",
            "grams": ri.grams,
            "kcal": ri.kcal,
            "optional": bool(ri.optional),
            "nonfood": bool(ri.nonfood),
            # Nenapárovaná surovina = řádek, který appka nezná. Rozhodnuté
            # ne-suroviny („alobal", „na ozdobu") se sem nepočítají.
            "unmatched": ri.ingredient_id is None and not ri.nonfood,
        })
    return out


def recipe_payload(rec: Recipe, include_raw: bool = True) -> dict:
    ing_rows = _ingredient_rows(rec)
    metrics = corpus_audit.recipe_metrics(
        rec.title, rec.instructions, [r["raw_text"] for r in ing_rows]
    )
    raw = ""
    if include_raw and rec.raw_json:
        raw = rec.raw_json
        if len(raw) > _RAW_TRUNC:
            raw = raw[:_RAW_TRUNC] + f"\n… (zkráceno, celkem {len(rec.raw_json)} znaků)"
    return {
        "id": rec.id,
        "title": rec.title or "",
        "original_title": rec.original_title or "",
        "source_url": rec.source_url or "",
        "source_domain": rec.source_domain or "",
        "created_at": rec.created_at.isoformat() if rec.created_at else "",
        "instructions": rec.instructions or "",
        "original_instructions": rec.original_instructions or "",
        "translated": bool(rec.original_instructions or rec.original_title),
        "servings": rec.servings,
        "total_time": rec.total_time,
        "rating": rec.rating,
        "rating_count": rec.rating_count,
        "category": rec.category or "",
        "kcal_per_serving": rec.kcal_per_serving,
        "kcal_per_100g": rec.kcal_per_100g,
        "total_weight_g": rec.total_weight_g,
        "feed_score": rec.feed_score,
        "hidden": bool(rec.hidden),
        "ing_total": rec.ing_total,
        "title_key": rec.title_key or "",
        "enrichment_status": rec.enrichment_status or "",
        "enrichment_error": rec.enrichment_error or "",
        "tags": [
            {"namespace": t.namespace, "slug": t.slug, "label": t.label_cs}
            for t in sorted(rec.tags, key=lambda t: (t.namespace, t.slug))
        ],
        "ingredients": ing_rows,
        "n_unmatched": sum(1 for r in ing_rows if r["unmatched"]),
        "metrics": metrics,
        "raw_json": raw,
    }


# ─── HTML ────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def _num(v, digits: int = 1) -> str:
    """Číslo pro čtení. Nuly se ořezávají jen ZA desetinnou tečkou – jinak by
    se z 200 g stalo „2" a z 700 kcal „7"."""
    if v is None:
        return "–"
    if isinstance(v, float):
        s = f"{v:.{digits}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s
    return str(v)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """České skloňování po číslovce: 1 surovina, 2–4 suroviny, 5+ surovin."""
    word = one if n == 1 else (few if 2 <= n <= 4 else many)
    return f"{n} {word}"


_CSS = """
:root{--bg:#faf9f7;--card:#fff;--line:#e3e0da;--ink:#26231f;--dim:#77706a;
      --warn:#b4341f;--ok:#2c6e49;--accent:#8a5a2b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);
       border-bottom:1px solid var(--line);padding:14px 20px}
h1{margin:0 0 4px;font-size:18px}
.meta{color:var(--dim);font-size:12px}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-top:10px}
.controls input[type=search]{flex:1;min-width:220px;padding:7px 10px;
       border:1px solid var(--line);border-radius:7px;font-size:13px}
.controls label{font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer}
main{padding:20px;max-width:1200px;margin:0 auto}
.rec{background:var(--card);border:1px solid var(--line);border-radius:10px;
     padding:16px 18px;margin-bottom:18px}
.rec h2{margin:0 0 2px;font-size:16px}
.rec h2 .orig{color:var(--dim);font-weight:400;font-size:13px}
.sub{color:var(--dim);font-size:12px;margin-bottom:10px;word-break:break-all}
.sub a{color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0}
.chip{background:#f0ece4;border-radius:99px;padding:2px 9px;font-size:12px}
.chip.ns{background:#e6eef0}
.chip.bad{background:#fbe6e2;color:var(--warn)}
.chip.good{background:#e4f0e8;color:var(--ok)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px}
.cols.one{grid-template-columns:1fr}
.col h3{margin:0 0 5px;font-size:12px;text-transform:uppercase;
        letter-spacing:.05em;color:var(--dim)}
.text{white-space:pre-wrap;background:#fbfaf8;border:1px solid var(--line);
      border-radius:7px;padding:10px;font-size:13px;max-height:340px;overflow:auto}
.text.empty{color:var(--warn);font-style:italic}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);
      vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim)}
td.num{text-align:right;white-space:nowrap}
tr.unmatched td{background:#fdf3f1}
tr.nonfood td{color:var(--dim)}
details{margin-top:10px}
summary{cursor:pointer;color:var(--dim);font-size:12px}
details pre{background:#fbfaf8;border:1px solid var(--line);border-radius:7px;
     padding:10px;overflow:auto;max-height:300px;font-size:12px}
.empty-state{color:var(--dim);text-align:center;padding:40px}
@media (max-width:800px){.cols{grid-template-columns:1fr}}
"""

# Filtry běží nad už vykreslenými kartami – žádný fetch, soubor funguje
# i bez serveru (stáhne se a otevře lokálně).
_JS = """
const q=document.getElementById('q'),cards=[...document.querySelectorAll('.rec')],
      flags=[...document.querySelectorAll('.controls input[type=checkbox]')],
      cnt=document.getElementById('cnt');
function apply(){
  const t=q.value.trim().toLowerCase();
  const on=flags.filter(f=>f.checked).map(f=>f.dataset.flag);
  let n=0;
  for(const c of cards){
    const okT=!t||c.dataset.search.includes(t);
    const okF=on.every(f=>c.dataset[f]==='1');
    const show=okT&&okF; c.hidden=!show; if(show)n++;
  }
  cnt.textContent=n;
}
q.addEventListener('input',apply);flags.forEach(f=>f.addEventListener('change',apply));
apply();
"""


def _chips(r: dict) -> str:
    out = []
    m = r["metrics"]
    if r["translated"]:
        out.append('<span class="chip">přeloženo</span>')
    if r["n_unmatched"]:
        out.append(f'<span class="chip bad">{r["n_unmatched"]}× nenapárováno</span>')
    if m["has_empty_instr"]:
        out.append('<span class="chip bad">prázdný postup</span>')
    if m["has_no_action"]:
        out.append('<span class="chip bad">žádná akce v postupu</span>')
    if r["hidden"]:
        out.append('<span class="chip bad">skryto</span>')
    if r["enrichment_error"]:
        out.append(f'<span class="chip bad">chyba: {_e(r["enrichment_error"][:80])}</span>')
    for t in r["tags"]:
        out.append(f'<span class="chip ns">{_e(t["namespace"])}: {_e(t["label"])}</span>')
    return "".join(out)


def _metrics_row(r: dict) -> str:
    m = r["metrics"]
    cov = m["ingr_coverage"]
    cls = "bad" if cov < 0.5 else ("good" if cov >= 0.8 else "")
    cells = [
        (f'<span class="chip {cls}">pokrytí surovin {cov:.0%}</span>'),
        f'<span class="chip">{_plural(m["n_ingredients"], "surovina", "suroviny", "surovin")}</span>',
        f'<span class="chip">{_plural(m["n_steps"], "krok", "kroky", "kroků")}</span>',
        f'<span class="chip">{_plural(m["instr_chars"], "znak", "znaky", "znaků")}</span>',
        f'<span class="chip">{_plural(m["n_cook_verbs"], "vařicí sloveso", "vařicí slovesa", "vařicích sloves")}</span>',
        f'<span class="chip">{_plural(m["n_prep_verbs"], "úkon", "úkony", "úkonů")}</span>',
    ]
    if r["feed_score"] is not None:
        cells.append(f'<span class="chip">skóre {_num(r["feed_score"], 2)}</span>')
    if r["rating"] is not None:
        cells.append(f'<span class="chip">{_num(r["rating"], 1)}★ '
                     f'({r["rating_count"] or 0})</span>')
    if m["has_time"]:
        cells.append('<span class="chip">má čas</span>')
    if m["has_temp"]:
        cells.append('<span class="chip">má teplotu</span>')
    if r["kcal_per_serving"] is not None:
        cells.append(f'<span class="chip">{_num(r["kcal_per_serving"], 0)} kcal/porce</span>')
    return "".join(cells)


def _ing_table(r: dict) -> str:
    if not r["ingredients"]:
        return '<p class="text empty">Recept nemá žádné suroviny.</p>'
    show_orig = any(i["original_raw_text"] for i in r["ingredients"])
    head = ["řádek v receptu"]
    if show_orig:
        head.append("originál (před překladem)")
    head += ["napárováno na", "množství", "gramy", "kcal"]
    rows = []
    for i in r["ingredients"]:
        cls = "unmatched" if i["unmatched"] else ("nonfood" if i["nonfood"] else "")
        cells = [f'<td>{_e(i["raw_text"])}</td>']
        if show_orig:
            cells.append(f'<td>{_e(i["original_raw_text"]) or "–"}</td>')
        if i["matched"]:
            matched = _e(i["matched"])
            if i["matched_category"]:
                matched += f'<br><span class="orig">{_e(i["matched_category"])}</span>'
        elif i["nonfood"]:
            matched = '<em>ne-surovina</em>'
        else:
            matched = '<strong>— nenapárováno —</strong>'
        amount = f'{_num(i["amount"], 2)} {_e(i["unit"])}'.strip()
        cells += [
            f'<td>{matched}{" <em>(volitelné)</em>" if i["optional"] else ""}</td>',
            f'<td class="num">{amount or "–"}</td>',
            f'<td class="num">{_num(i["grams"], 0)}</td>',
            f'<td class="num">{_num(i["kcal"], 0)}</td>',
        ]
        rows.append(f'<tr class="{cls}">{"".join(cells)}</tr>')
    return (f'<table><thead><tr>{"".join(f"<th>{h}</th>" for h in head)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _recipe_html(r: dict) -> str:
    m = r["metrics"]
    orig_title = (f' <span class="orig">← {_e(r["original_title"])}</span>'
                  if r["original_title"] and r["original_title"] != r["title"] else "")
    # Postup: dva sloupce jen když se opravdu je s čím porovnávat.
    body = _e(r["instructions"]) or "— prázdné —"
    body_cls = "text" + ("" if r["instructions"] else " empty")
    if r["original_instructions"]:
        cols = (
            f'<div class="cols">'
            f'<div class="col"><h3>originál (před překladem)</h3>'
            f'<div class="text">{_e(r["original_instructions"])}</div></div>'
            f'<div class="col"><h3>jak to vidíš v appce</h3>'
            f'<div class="{body_cls}">{body}</div></div></div>'
        )
    else:
        cols = (f'<div class="cols one"><div class="col">'
                f'<h3>postup (nepřekládáno – zobrazený text je originál)</h3>'
                f'<div class="{body_cls}">{body}</div></div></div>')

    raw = ""
    if r["raw_json"]:
        raw = (f'<details><summary>Syrová data ze scraperu (raw_json)</summary>'
               f'<pre>{_e(r["raw_json"])}</pre></details>')

    search = " ".join([r["title"], r["original_title"], r["source_domain"],
                       r["instructions"][:2000],
                       " ".join(i["raw_text"] for i in r["ingredients"]),
                       " ".join(t["label"] for t in r["tags"])]).lower()

    return f"""<article class="rec"
  data-search="{_e(search)}"
  data-translated="{int(r["translated"])}"
  data-unmatched="{int(bool(r["n_unmatched"]))}"
  data-noinstr="{int(bool(m["has_empty_instr"]))}"
  data-lowcov="{int(m["ingr_coverage"] < 0.5)}">
  <h2>{_e(r["title"])}{orig_title}</h2>
  <div class="sub">#{r["id"]} · {_e(r["source_domain"]) or "bez domény"} ·
    <a href="{_e(r["source_url"])}" target="_blank" rel="noopener">{_e(r["source_url"])}</a>
  </div>
  <div class="chips">{_metrics_row(r)}</div>
  <div class="chips">{_chips(r)}</div>
  {cols}
  {_ing_table(r)}
  {raw}
</article>"""


def _write_html(recipes: list[dict], info: dict) -> None:
    gen = datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")
    n_unmatched = sum(1 for r in recipes if r["n_unmatched"])
    n_translated = sum(1 for r in recipes if r["translated"])
    n_lowcov = sum(1 for r in recipes if r["metrics"]["ingr_coverage"] < 0.5)
    n_noinstr = sum(1 for r in recipes if r["metrics"]["has_empty_instr"])

    head = f"""<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kuchařka – kontrola zpracování receptů</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>Kontrola zpracování receptů</h1>
  <div class="meta">
    {len(recipes)} receptů · výběr <strong>{_e(info["pick"])}</strong>
    ({_e(PICKS.get(info["pick"], ""))}){f' · doména {_e(info["domain"])}' if info.get("domain") else ""}
    · seed {info["seed"]} · z {info["total_recipes"]} receptů v databázi
    · vygenerováno {gen}
  </div>
  <div class="meta">
    Nenapárovaná surovina: {n_unmatched} · přeloženo: {n_translated} ·
    pokrytí pod 50 %: {n_lowcov} · prázdný postup: {n_noinstr}
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="Hledat v názvu, postupu, surovinách…">
    <label><input type="checkbox" data-flag="unmatched"> nenapárovaná surovina</label>
    <label><input type="checkbox" data-flag="translated"> přeložené</label>
    <label><input type="checkbox" data-flag="lowcov"> pokrytí pod 50 %</label>
    <label><input type="checkbox" data-flag="noinstr"> prázdný postup</label>
    <span class="meta">zobrazeno <strong id="cnt">0</strong></span>
  </div>
</header>
<main>"""
    # Karty se zapisují po jedné. Poskládat celý dokument do jednoho řetězce
    # by znamenalo držet ho v paměti dvakrát (spojení + výsledek) – u dvou
    # tisíc receptů je to skoro 40 MB navíc úplně zbytečně.
    with HTML_PATH.open("w", encoding="utf-8") as fh:
        fh.write(head)
        if not recipes:
            fh.write('<p class="empty-state">Výběru neodpovídá žádný recept.</p>')
        for r in recipes:
            fh.write(_recipe_html(r))
            fh.write("\n")
        fh.write(f"</main>\n<script>{_JS}</script>\n</body></html>")


# ─── XML ─────────────────────────────────────────────────────────────────────

def _x(tag: str, value, indent: int = 4) -> str:
    if value is None or value == "":
        return f'{" " * indent}<{tag}/>'
    return f'{" " * indent}<{tag}>{xml_escape(str(value))}</{tag}>'


def _recipe_xml(r: dict) -> str:
    parts = [f'  <recipe id="{r["id"]}" translated="{str(r["translated"]).lower()}">']
    for tag in ("title", "original_title", "source_url", "source_domain",
                "created_at", "category", "title_key", "enrichment_status",
                "enrichment_error", "servings", "total_time", "rating",
                "rating_count", "kcal_per_serving", "kcal_per_100g",
                "total_weight_g", "feed_score", "ing_total",
                "instructions", "original_instructions"):
        parts.append(_x(tag, r[tag]))
    parts.append('    <metrics>')
    for k, v in r["metrics"].items():
        parts.append(_x(k, ",".join(v) if isinstance(v, list) else v, 6))
    parts.append('    </metrics>')
    parts.append('    <tags>')
    for t in r["tags"]:
        parts.append(f'      <tag namespace="{xml_escape(t["namespace"])}" '
                     f'slug="{xml_escape(t["slug"])}">{xml_escape(t["label"])}</tag>')
    parts.append('    </tags>')
    parts.append('    <ingredients>')
    for i in r["ingredients"]:
        attrs = (f'unmatched="{str(i["unmatched"]).lower()}" '
                 f'nonfood="{str(i["nonfood"]).lower()}" '
                 f'optional="{str(i["optional"]).lower()}"')
        parts.append(f'      <ingredient {attrs}>')
        for tag in ("raw_text", "original_raw_text", "matched",
                    "matched_category", "amount", "unit", "grams", "kcal"):
            parts.append(_x(tag, i[tag], 8))
        parts.append('      </ingredient>')
    parts.append('    </ingredients>')
    if r["raw_json"]:
        parts.append(_x("raw_json", r["raw_json"]))
    parts.append('  </recipe>')
    return "\n".join(parts)


def _write_xml(recipes: list[dict], info: dict) -> None:
    # Zapisuje se po receptech ze stejného důvodu jako u HTML – ať dokument
    # nemusí být celý v paměti.
    with XML_PATH.open("w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write(f'<export pick="{xml_escape(info["pick"])}" seed="{info["seed"]}" '
                 f'count="{len(recipes)}" total_recipes="{info["total_recipes"]}" '
                 f'generated="{datetime.now(timezone.utc).isoformat()}">\n')
        for r in recipes:
            fh.write(_recipe_xml(r))
            fh.write("\n")
        fh.write('</export>')


# ─── Stav běhu (stejný vzor jako ostatní úlohy na pozadí) ────────────────────

_lock = threading.Lock()
_state: dict = {
    "running": False, "done": 0, "total": 0, "error": None,
    "finished_at": None, "duration_s": None, "pick": None, "seed": None,
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["picks"] = PICKS
    for key, path in (("html", HTML_PATH), ("xml", XML_PATH)):
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


# ─── Běh ─────────────────────────────────────────────────────────────────────

def run(limit: int = 100, seed: int = 42, pick: str = "random",
        domain: str | None = None, include_raw: bool = True) -> dict:
    """Vyexportuj vzorek receptů do HTML + XML. Nic nezapisuje do databáze."""
    if pick not in PICKS:
        raise ValueError(f"neznámý výběr {pick!r}; možnosti: {', '.join(PICKS)}")
    if limit > MAX_LIMIT:
        log.warning(
            "Export receptů: požadováno %s receptů, snižuji na %s. Víc se do "
            "jednoho HTML nevejde (~9 kB a ~70 MB paměti na tisíc receptů) a "
            "stejně se to nedá přečíst – na celý korpus je corpus_audit.",
            limit, MAX_LIMIT,
        )
        limit = MAX_LIMIT
    limit = max(1, limit)
    started = time.monotonic()
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        total_recipes = db.scalar(select(func.count(Recipe.id))) or 0
        ids = pick_ids(db, pick, limit, seed, domain)
        _set(total=len(ids), done=0, error=None, pick=pick, seed=seed)

        recipes: list[dict] = []
        # Suroviny i tagy jedním dotazem na dávku – bez toho by to bylo N+1
        # a stovka receptů by znamenala stovky dotazů.
        for chunk_start in range(0, len(ids), 50):
            chunk = ids[chunk_start:chunk_start + 50]
            rows = db.scalars(
                select(Recipe)
                .where(Recipe.id.in_(chunk))
                .options(selectinload(Recipe.ingredients)
                         .selectinload(RecipeIngredient.ingredient),
                         selectinload(Recipe.tags))
                .order_by(Recipe.id)
            ).all()
            for rec in rows:
                recipes.append(recipe_payload(rec, include_raw))
            _set(done=len(recipes))

        # Nejhorší nahoru: kdo si export otevře, chce vidět problémy, ne
        # průměr. Řadí se podle pokrytí surovin, pak podle počtu
        # nenapárovaných řádků.
        recipes.sort(key=lambda r: (r["metrics"]["ingr_coverage"], -r["n_unmatched"]))

        info = {"pick": pick, "seed": seed, "domain": domain,
                "total_recipes": total_recipes}
        _write_html(recipes, info)
        _write_xml(recipes, info)

        duration = round(time.monotonic() - started, 1)
        _set(duration_s=duration, error=None)
        log.info("Export receptů: %s receptů (%s) za %s s → %s",
                 len(recipes), pick, duration, HTML_PATH.name)
        return {
            "count": len(recipes), "pick": pick, "seed": seed,
            "duration_s": duration,
            "html_path": str(HTML_PATH), "xml_path": str(XML_PATH),
        }
    finally:
        db.close()


def run_async(limit: int = 100, seed: int = 42, pick: str = "random",
              domain: str | None = None, include_raw: bool = True) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, total=0, error=None, finished_at=None)

    def _worker():
        try:
            run(limit=limit, seed=seed, pick=pick, domain=domain,
                include_raw=include_raw)
        except Exception as exc:  # noqa: BLE001 – vlákno nesmí umřít potichu
            log.error("export receptů selhal: %s\n%s", exc, traceback.format_exc())
            _set(error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _set(running=False, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="recipe-export").start()
    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Čitelný export receptů ke kontrole")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--pick", default="random", choices=sorted(PICKS))
    ap.add_argument("--domain", default=None)
    ap.add_argument("--no-raw", action="store_true", help="vynech raw_json")
    args = ap.parse_args()
    print(json.dumps(
        run(limit=args.limit, seed=args.seed, pick=args.pick,
            domain=args.domain, include_raw=not args.no_raw),
        ensure_ascii=False, indent=2))
