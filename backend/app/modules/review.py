"""Ruční kontrola receptů – štítky, stránkování, statistika.

K čemu to je: `corpus_audit` spočítá, KOLIK receptů je podezřelých, ale co je
s konkrétním receptem, pozná jen člověk. Tenhle modul obsluhuje záložku
Kontrola: vytáhne stránku receptů se vším, co je k posouzení potřeba (obě
verze textu, výsledek párování surovin, metriky, tagy) a uloží, jak se člověk
rozhodl.

Sestavení dat jednoho receptu je společné s exportem do HTML/XML – bydlí
v `recipe_export.recipe_payload`, ať se to nerozejde.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import Recipe, RecipeIngredient, RecipeReview
from . import recipe_export

log = logging.getLogger("kucharka.review")

# Pevná nabídka štítků. `hides` znamená, že označení recept zároveň skryje
# z výpisů – „tohle není recept" má mít okamžitý efekt, jinak by se člověk
# prokousal korpusem a nic by se nezměnilo.
LABELS: list[dict] = [
    {"slug": "zkontrolovano", "label": "Zkontrolováno", "hides": False,
     "hint": "Recept je v pořádku, znovu ho neukazovat."},
    {"slug": "neni-recept", "label": "Není recept", "hides": True,
     "hint": "Zdobení dortu, návod na nápoj, reklama… Skryje se z výpisů."},
    {"slug": "spatny-preklad", "label": "Špatný překlad", "hides": False,
     "hint": "Text je přeložený nesmyslně nebo jen zpola."},
    {"slug": "spatne-suroviny", "label": "Špatné suroviny", "hides": False,
     "hint": "Řádky surovin jsou špatně napárované nebo rozparsované."},
    {"slug": "chybi-postup", "label": "Chybí postup", "hides": False,
     "hint": "Postup je prázdný, useknutý nebo to je jen upoutávka."},
    {"slug": "duplicita", "label": "Duplicita", "hides": True,
     "hint": "Tentýž recept už v korpusu je. Skryje se z výpisů."},
]

_BY_SLUG = {d["slug"]: d for d in LABELS}
_HIDING = {d["slug"] for d in LABELS if d["hides"]}


def parse_labels(raw: str | None) -> list[str]:
    return [s for s in (raw or "").split(",") if s in _BY_SLUG]


def clean_labels(labels: list[str]) -> list[str]:
    """Zahoď neznámé štítky a duplicity, zachovej pořadí z LABELS."""
    wanted = set(labels)
    return [d["slug"] for d in LABELS if d["slug"] in wanted]


# ─── Čtení stránky ke kontrole ───────────────────────────────────────────────

def page(db: Session, *, pick: str = "random", domain: str | None = None,
         only_unreviewed: bool = False, page_no: int = 1, per_page: int = 10,
         seed: int = 42, include_raw: bool = False) -> dict:
    """Jedna stránka receptů ke kontrole.

    Výběr (`pick`) je stejný jako u exportu – míří na recepty, kde se dá čekat
    problém. Uvnitř výběru se řadí podle id, aby stránkování bylo stabilní:
    kdyby se řadilo podle pokrytí surovin, uložení štítku by přeházelo pořadí
    a člověk by přeskakoval nebo viděl totéž dvakrát.
    """
    ids = recipe_export.pick_ids(db, pick, limit=None, seed=seed, domain=domain)

    if only_unreviewed:
        done = set(db.scalars(select(RecipeReview.recipe_id)))
        ids = [i for i in ids if i not in done]

    total = len(ids)
    per_page = max(1, min(per_page, 50))
    pages = max(1, -(-total // per_page))
    page_no = max(1, min(page_no, pages))
    chunk = ids[(page_no - 1) * per_page: page_no * per_page]

    rows = db.scalars(
        select(Recipe)
        .where(Recipe.id.in_(chunk))
        .options(selectinload(Recipe.ingredients)
                 .selectinload(RecipeIngredient.ingredient),
                 selectinload(Recipe.tags))
        .order_by(Recipe.id)
    ).all() if chunk else []

    reviews = {
        r.recipe_id: r for r in db.scalars(
            select(RecipeReview).where(RecipeReview.recipe_id.in_(chunk))
        )
    } if chunk else {}

    items = []
    for rec in rows:
        payload = recipe_export.recipe_payload(rec, include_raw=include_raw)
        rev = reviews.get(rec.id)
        payload["review"] = {
            "labels": parse_labels(rev.labels if rev else ""),
            "note": (rev.note if rev else "") or "",
            "reviewed_at": rev.reviewed_at.isoformat() if rev else None,
        }
        items.append(payload)

    return {
        "items": items, "total": total, "page": page_no, "pages": pages,
        "per_page": per_page, "pick": pick, "domain": domain,
        "only_unreviewed": only_unreviewed,
    }


# ─── Zápis rozhodnutí ────────────────────────────────────────────────────────

def save(db: Session, recipe_id: int, labels: list[str],
         note: str | None = None) -> dict:
    """Ulož rozhodnutí o receptu. Prázdný seznam štítků i poznámka = kontrola
    se zruší a recept se vrátí mezi nezkontrolované."""
    rec = db.get(Recipe, recipe_id)
    if rec is None:
        raise LookupError(f"recept {recipe_id} neexistuje")

    labels = clean_labels(labels)
    note = (note or "").strip() or None

    rev = db.scalar(select(RecipeReview).where(RecipeReview.recipe_id == recipe_id))
    if not labels and not note:
        if rev is not None:
            db.delete(rev)
    else:
        if rev is None:
            rev = RecipeReview(recipe_id=recipe_id)
            db.add(rev)
        rev.labels = ",".join(labels)
        rev.note = note

    # Štítky typu „není recept" mají recept rovnou schovat z výpisů; po jejich
    # odebrání se zase odkryje. Jiné cesty ke `hidden` (ruční tlačítko
    # v detailu) tím nepřepisujeme – měníme ho jen když se stav opravdu mění.
    should_hide = bool(_HIDING & set(labels))
    if should_hide and not rec.hidden:
        rec.hidden = True
    elif not should_hide and rec.hidden and rev is not None:
        # Odkrýt jen tehdy, když skrytí plyne z kontroly. U receptu, který
        # nikdy kontrolou neprošel, se `hidden` nechává na pokoji.
        rec.hidden = False

    db.commit()
    return {
        "recipe_id": recipe_id, "labels": labels, "note": note or "",
        "hidden": bool(rec.hidden),
    }


# ─── Statistika ──────────────────────────────────────────────────────────────

def stats(db: Session) -> dict:
    total = db.scalar(select(func.count(Recipe.id))) or 0
    reviewed = db.scalar(select(func.count(RecipeReview.id))) or 0
    counts = {d["slug"]: 0 for d in LABELS}
    for raw in db.scalars(select(RecipeReview.labels)):
        for slug in parse_labels(raw):
            counts[slug] += 1
    return {
        "total_recipes": total,
        "reviewed": reviewed,
        "remaining": max(0, total - reviewed),
        "by_label": counts,
        "labels": LABELS,
    }
