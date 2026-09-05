"""Rodičovské vazby mezi surovinami – „arborio rýže" patří pod „rýže".

Proč: ve „Vařím z" se dala vybrat jen konkrétní surovina. Kdo měl doma
„rýži", nenašel recept na rizoto s „arborio rýží", protože to jsou dva různé
záznamy slovníku bez jakéhokoli vztahu. Totéž „olej"/„olivový olej",
„mouka"/„hladká mouka", „mléko"/„plnotučné mléko".

Vazba se odvozuje z NÁZVU, bez modelu: název suroviny se rozloží na kmeny
(stejný stemmer jako hledání) a rodičem je ta surovina, jejíž kmeny jsou
vlastní podmnožinou. „arborio rýže" = {arborio, ryz} ⊃ {ryz} = „rýže".
Z několika kandidátů vyhrává ten nejkonkrétnější, tedy s nejvíc kmeny:
u „kuřecí prsa bez kosti" je lepší rodič „kuřecí prsa" než „kuřecí".

Co tahle metoda NEUMÍ: vazby, které z názvu nejdou vyčíst. „kuřecí křidélka"
se pod „kuřecí maso" samo nedostane, protože {kurc, kridelk} není nadmnožina
{kurc, mas}. Na to by byl potřeba model nebo ruční zásah; hrubé zařazení
zatím drží kategorie z číselníku (modules/taxonomy).
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import Ingredient
from . import textnorm

log = logging.getLogger("kucharka.ingredient_tree")

# Kmeny, které samy o sobě surovinu neurčují. Bez tohohle by „sůl" dostala
# za rodiče cokoli jednoslovného a hlavně by vznikaly nesmyslné vazby přes
# obecná přídavná jména („čerstvý", „mletý").
_STOP_STEMS = frozenset("""
cerstv suse mlet hrub jemn cel krajen sekan nakrajen stroujan
bio light domac hotov prirodn klasick obycejn velk mal stredn
""".split())

# Kolik nejvýš úrovní se prochází při rozbalování potomků. Pojistka proti
# cyklu, kdyby data někdo ručně propojil dokola.
_MAX_DEPTH = 6


def _stems(name: str) -> frozenset[str]:
    return frozenset(t for t in textnorm.tokens(name or "") if t not in _STOP_STEMS)


def build(dry_run: bool = False) -> dict:
    """Spočítej a ulož rodiče pro všechny suroviny.

    Běží bez modelu nad názvy, takže je to otázka vteřin i pro 12 tisíc
    surovin. Idempotentní – druhý běh nic nezmění.
    """
    started = time.monotonic()
    db = SessionLocal()
    try:
        rows = db.execute(select(Ingredient.id, Ingredient.name_cs)).all()
        stems: dict[int, frozenset[str]] = {}
        for ing_id, name in rows:
            s = _stems(name)
            if s:
                stems[ing_id] = s

        # Index podle kmenů: kandidáti na rodiče se hledají jen mezi
        # surovinami, které sdílejí aspoň jeden kmen. Porovnávat každou
        # s každou by u 12 tisíc znamenalo 144 milionů dvojic.
        by_stem: dict[str, list[int]] = {}
        for ing_id, s in stems.items():
            for stem in s:
                by_stem.setdefault(stem, []).append(ing_id)

        parents: dict[int, int | None] = {}
        for ing_id, s in stems.items():
            best: tuple[int, int] | None = None  # (počet kmenů, id)
            seen: set[int] = set()
            for stem in s:
                for cand in by_stem.get(stem, ()):
                    if cand == ing_id or cand in seen:
                        continue
                    seen.add(cand)
                    cs = stems[cand]
                    # Vlastní podmnožina = kandidát je obecnější.
                    if cs < s and (best is None or len(cs) > best[0]):
                        best = (len(cs), cand)
            parents[ing_id] = best[1] if best else None

        # Cyklus: dvě suroviny se stejnou množinou kmenů se nemůžou stát
        # rodiči navzájem, protože se vyžaduje VLASTNÍ podmnožina. Přesto to
        # radši ověřím – jeden špatný řádek by zacyklil rozbalování.
        parents = _break_cycles(parents)

        current = {ing_id: pid for ing_id, pid in db.execute(
            select(Ingredient.id, Ingredient.parent_id)
        ).all()}
        changes = [
            {"id": ing_id, "parent_id": parents.get(ing_id)}
            for ing_id in current
            if current.get(ing_id) != parents.get(ing_id)
        ]
        if changes and not dry_run:
            db.execute(update(Ingredient), changes)
            db.commit()

        linked = sum(1 for v in parents.values() if v is not None)
        duration = round(time.monotonic() - started, 1)
        log.info("Strom surovin: %s z %s má rodiče, %s změn za %s s%s",
                 linked, len(rows), len(changes), duration,
                 " (nanečisto)" if dry_run else "")
        return {"total": len(rows), "linked": linked, "changed": len(changes),
                "duration_s": duration, "dry_run": dry_run}
    finally:
        db.close()


def _break_cycles(parents: dict[int, int | None]) -> dict[int, int | None]:
    out = dict(parents)
    for start in list(out):
        seen = {start}
        node = out.get(start)
        while node is not None:
            if node in seen:
                out[start] = None
                break
            seen.add(node)
            node = out.get(node)
    return out


# ─── Rozbalování výběru ──────────────────────────────────────────────────────

def expand(db, ingredient_ids) -> set[int]:
    """Doplň k vybraným surovinám všechny jejich potomky.

    „Mám doma rýži" má najít i recept s arborio rýží. Opačně to neplatí:
    kdo vybere „arborio rýže", nedostane recepty na obyčejnou rýži –
    konkrétní surovinou obecnou nahradit nejde.
    """
    out = set(ingredient_ids or ())
    if not out:
        return out
    frontier = set(out)
    for _ in range(_MAX_DEPTH):
        kids = set(db.scalars(
            select(Ingredient.id).where(Ingredient.parent_id.in_(frontier))
        )) - out
        if not kids:
            break
        out |= kids
        frontier = kids
    return out


def children_of(db, ingredient_id: int) -> list[Ingredient]:
    return list(db.scalars(
        select(Ingredient).where(Ingredient.parent_id == ingredient_id)
        .order_by(Ingredient.name_cs)
    ))


def build_async(dry_run: bool = False) -> bool:
    threading.Thread(
        target=build, args=(dry_run,), daemon=True, name="ingredient-tree",
    ).start()
    return True
