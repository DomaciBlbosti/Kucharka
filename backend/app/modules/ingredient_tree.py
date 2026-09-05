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

from ..config import settings
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


# ─── Doplnění vazeb modelem ──────────────────────────────────────────────────
#
# Odvození z názvu nechytne vazby, které v názvu nejsou: „kuřecí křidélka"
# nemá „maso" ve jméně, takže se pod „kuřecí maso" samo nedostane. Tohle je
# doplňuje modelem – ale jen tam, kde deterministický krok nic nenašel, a jen
# uvnitř jedné kategorie z číselníku. Model tak nevybírá z 12 tisíc surovin,
# ale z pár desítek, které k sobě opravdu patří.

_LLM_BATCH = 20        # kolik surovin se ptá naráz
_LLM_CANDIDATES = 40   # kolik nejobecnějších surovin se nabídne jako rodiče

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "p": {"type": "integer"}},
                "required": ["i", "p"],
            },
        }
    },
    "required": ["items"],
}

_llm_lock = threading.Lock()
_llm_state: dict = {
    "running": False, "done": 0, "total": 0, "linked": 0, "errors": 0,
    "finished_at": None, "error": None,
}


def llm_status() -> dict:
    with _llm_lock:
        return dict(_llm_state)


def _llm_set(**kw):
    with _llm_lock:
        _llm_state.update(kw)


def _llm_inc(key: str, by: int = 1):
    with _llm_lock:
        _llm_state[key] = _llm_state.get(key, 0) + by


def _ask_group(category: str, candidates: list[tuple[int, str]],
               items: list[tuple[int, str]]) -> dict[int, int]:
    """Zeptej se modelu na rodiče pro jednu dávku. Vrací {id potomka: id rodiče}."""
    from . import llmclient

    cand_txt = "\n".join(f"{n}. {name}" for n, (_id, name) in enumerate(candidates))
    item_txt = "\n".join(f"{n}. {name}" for n, (_id, name) in enumerate(items))
    prompt = (
        f"Suroviny z kategorie „{category}“. U každé potraviny ze seznamu B urči, "
        "která potravina ze seznamu A je její OBECNĚJŠÍ nadřazená surovina.\n"
        "Příklad: „kuřecí křidélka“ patří pod „kuřecí maso“; „lučina“ patří pod "
        "„tvarohový sýr“.\n"
        "Odpověz -1, když žádná ze seznamu A nadřazená není, nebo když by šlo "
        "o totéž. Nadřazená surovina musí být OBECNĚJŠÍ, ne jiný druh téhož.\n\n"
        f"SEZNAM A (možní rodiče):\n{cand_txt}\n\n"
        f"SEZNAM B (co zařadit):\n{item_txt}\n\n"
        "Odpověz POUZE JSON {\"items\":[{\"i\":<číslo z B>,\"p\":<číslo z A nebo -1>}]}."
    )
    out = llmclient.structured_json(
        prompt, schema=_LLM_SCHEMA,
        timeout=max(settings.http_timeout, settings.llm_match_timeout_s),
        num_ctx=8192, component="strom surovin",
    )
    if out is None:
        _llm_inc("errors")
        return {}

    pairs: dict[int, int] = {}
    for it in out.get("items", []):
        try:
            i = int(it.get("i"))
            pcand = int(it.get("p"))
        except Exception:  # noqa: BLE001 – model vrátil nečíslo
            continue
        if not (0 <= i < len(items)) or pcand < 0:
            continue
        if not (0 <= pcand < len(candidates)):
            continue  # vymyšlený index – radši nic
        child_id, parent_id = items[i][0], candidates[pcand][0]
        if child_id != parent_id:
            pairs[child_id] = parent_id
    return pairs


def llm_link(limit_categories: int | None = None) -> dict:
    """Doplň chybějící rodiče modelem, kategorii po kategorii.

    Nesahá na vazby z `build()` – ty jsou odvozené z názvu a spolehlivé.
    Řeší jen suroviny, které po něm zůstaly bez rodiče.
    """
    from . import llmclient

    if not llmclient.is_available():
        return {"skipped": "LLM není dostupné"}

    _llm_set(running=True, done=0, total=0, linked=0, errors=0, error=None,
             finished_at=None)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Ingredient.id, Ingredient.name_cs, Ingredient.category_path,
                   Ingredient.parent_id)
        ).all()
        by_cat: dict[str, list] = {}
        for r in rows:
            cat = (r.category_path or "").strip()
            if cat:
                by_cat.setdefault(cat, []).append(r)

        # Kategorie s jedinou surovinou nemají co řešit.
        groups = [(c, rs) for c, rs in by_cat.items() if len(rs) > 1]
        groups.sort(key=lambda x: -len(x[1]))
        if limit_categories:
            groups = groups[:limit_categories]
        _llm_set(total=sum(len(rs) for _c, rs in groups))

        linked_total = 0
        for category, rs in groups:
            # Kandidáti na rodiče = nejobecnější názvy, tedy ty s nejmíň
            # slovy. „kuřecí maso" je lepší rodič než „kuřecí maso mleté".
            ranked = sorted(rs, key=lambda r: (len(_stems(r.name_cs)), len(r.name_cs)))
            candidates = [(r.id, r.name_cs) for r in ranked[:_LLM_CANDIDATES]]
            items = [(r.id, r.name_cs) for r in rs if r.parent_id is None]
            if not items or len(candidates) < 2:
                _llm_inc("done", len(rs))
                continue

            found: dict[int, int] = {}
            for start in range(0, len(items), _LLM_BATCH):
                batch = items[start:start + _LLM_BATCH]
                found.update(_ask_group(category, candidates, batch))
                _llm_inc("done", len(batch))

            if not found:
                continue
            # Cyklus musí padnout dřív, než se to uloží – rozbalování výběru
            # by se na něm zaseklo.
            merged = {r.id: r.parent_id for r in rows}
            merged.update(found)
            merged = _break_cycles(merged)
            updates = [
                {"id": cid, "parent_id": merged.get(cid)}
                for cid in found
                if merged.get(cid) is not None
            ]
            if updates:
                db.execute(update(Ingredient), updates)
                db.commit()
                linked_total += len(updates)
                _llm_set(linked=linked_total)

        log.info("Strom surovin (model): doplněno %s vazeb v %s kategoriích.",
                 linked_total, len(groups))
        return {"linked": linked_total, "categories": len(groups),
                "errors": _llm_state["errors"]}
    except Exception as exc:  # noqa: BLE001
        _llm_set(error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        db.close()
        _llm_set(running=False, finished_at=time.time())


def llm_link_async(limit_categories: int | None = None) -> bool:
    with _llm_lock:
        if _llm_state["running"]:
            return False
        _llm_state["running"] = True

    def _worker():
        try:
            llm_link(limit_categories=limit_categories)
        except Exception as exc:  # noqa: BLE001 – vlákno nesmí umřít potichu
            log.error("doplnění stromu surovin modelem selhalo: %s", exc)
        finally:
            _llm_set(running=False, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="ingredient-tree-llm").start()
    return True
