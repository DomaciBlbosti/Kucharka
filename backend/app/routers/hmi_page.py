"""Statická HTML stránka pro E-ink/kuchyňský displej – žádné JS, velký
kontrastní text, žádné externí zdroje (funguje i na primitivním prohlížeči
nebo screenshot-bridge zařízení). Displej si ji sám pravidelně stahuje.
"""
from __future__ import annotations

from datetime import date as _date
from html import escape

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import MealPlanEntry, Recipe, ShoppingItem
from .hmi import _cooking_recipe_id, require_hmi

router = APIRouter(prefix="/hmi", tags=["hmi"])

_CSS = """
body{font-family:Georgia,'DejaVu Serif',serif;color:#000;background:#fff;
  margin:0;padding:28px 32px;max-width:760px}
h1{font-size:2rem;margin:0 0 6px;line-height:1.15}
h2{font-size:1.3rem;margin:28px 0 10px;border-bottom:2px solid #000;padding-bottom:4px}
.meta{font-size:1.05rem;color:#333;margin-bottom:18px}
ul,ol{font-size:1.15rem;line-height:1.6;padding-left:1.4em}
li{margin-bottom:6px}
.meal{font-size:1.2rem;margin:10px 0;padding:10px 0;border-bottom:1px solid #999}
.meal b{font-size:1.3rem}
.kcal{color:#333}
.empty{font-size:1.15rem;color:#444;margin-top:20px}
"""


def _page(body: str, refresh: int | None) -> str:
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        "<!doctype html><html lang='cs'><head><meta charset='utf-8'>"
        f"{meta_refresh}<style>{_CSS}</style></head><body>{body}</body></html>"
    )


@router.get("", response_class=HTMLResponse)
def hmi_page(
    refresh: int | None = Query(default=None, ge=0),
    _: bool = Depends(require_hmi),
    db: Session = Depends(get_db),
):
    rid = _cooking_recipe_id(db)
    if rid:
        r = db.scalar(
            select(Recipe).where(Recipe.id == rid).options(selectinload(Recipe.ingredients))
        )
        if r:
            return HTMLResponse(_page(_cooking_body(r), refresh))

    return HTMLResponse(_page(_today_body(db), refresh))


def _cooking_body(r: Recipe) -> str:
    ing = "".join(f"<li>{escape(ri.raw_text)}</li>" for ri in r.ingredients)
    steps_raw = [s.strip() for s in (r.instructions or "").split("\n") if s.strip()]
    steps = "".join(f"<li>{escape(s)}</li>" for s in steps_raw)
    servings = f"<span class='meta'>{r.servings} porce</span>" if r.servings else ""
    return (
        f"<h1>{escape(r.title)}</h1>{servings}"
        f"<h2>Suroviny</h2><ul>{ing or '<li>—</li>'}</ul>"
        f"<h2>Postup</h2><ol>{steps or '<li>—</li>'}</ol>"
    )


def _today_body(db: Session) -> str:
    d = _date.today()
    entries = db.scalars(
        select(MealPlanEntry)
        .where(MealPlanEntry.date == d)
        .options(selectinload(MealPlanEntry.recipe))
        .order_by(MealPlanEntry.id)
    ).all()
    order = {"snídaně": 0, "svačina": 1, "oběd": 2, "večeře": 3}
    entries = sorted(entries, key=lambda e: order.get(e.meal, 9))

    if entries:
        rows = []
        for e in entries:
            kcal = (
                round((e.recipe.kcal_per_serving or 0) * e.servings)
                if e.recipe.kcal_per_serving
                else None
            )
            kcal_s = f" <span class='kcal'>· {kcal} kcal</span>" if kcal else ""
            rows.append(
                f"<div class='meal'><b>{escape(e.meal)}</b> — "
                f"{escape(e.recipe.title)}{kcal_s}</div>"
            )
        meals_html = "".join(rows)
    else:
        meals_html = "<p class='empty'>Na dnešek nic naplánováno.</p>"

    items = db.scalars(
        select(ShoppingItem).where(ShoppingItem.checked == False).order_by(ShoppingItem.label)  # noqa: E712
    ).all()
    if items:
        shop = "".join(f"<li>{escape(i.label)}</li>" for i in items[:20])
        shop_html = f"<ul>{shop}</ul>"
        if len(items) > 20:
            shop_html += f"<p class='meta'>… a dalších {len(items) - 20}</p>"
    else:
        shop_html = "<p class='empty'>Nákupní seznam je prázdný.</p>"

    return (
        f"<h1>Dnes {d.strftime('%d.%m.%Y')}</h1>"
        f"<h2>Jídelníček</h2>{meals_html}"
        f"<h2>Nákupní seznam</h2>{shop_html}"
    )
