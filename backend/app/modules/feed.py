"""Doporučené pořadí receptů pro úvodní stránku – počítá se na pozadí.

Proč to vzniklo. Úvodní stránka řadila přes „smart", tedy podle počtu
chybějících surovin. Jenže recept, kterému se NENAPÁROVALA ani jedna
surovina, má `ing_total = 0`, takže mu chybí nula surovin – a vyšplhal se
nad všechno ostatní jako „Můžeš vařit". Výsledek: první stránka plná návodů
na zdobení dortů a míchaných nápojů, u kterých svítilo „suroviny
nenapárované". Vysoké hodnocení ze zdrojového webu je vyneslo úplně nahoru.

Druhý problém byl výkon: `missing` je počítaný výraz, takže `ORDER BY` nad
ním musí projít a setřídit celou tabulku, ať uživatel chce jakkoli malou
stránku. U 171 tisíc receptů se to nedá zachránit indexem.

Tenhle modul obojí řeší jedním sloupcem `recipe.feed_score`: spočítá se
dávkově na pozadí, je nad ním index a výpis pak jen čte prvních N řádků.
Nezávisí na spíži – pořadí je stejné pro každé načtení a dá se cachovat.

Skóre stojí na bayesovském průměru hodnocení, takže „5,0 od tří lidí"
nepřebije „4,6 od pěti set", a strhává body za vlastnosti, kvůli kterým
recept není k ničemu (žádná napárovaná surovina, prázdný postup).
"""
from __future__ import annotations

import logging
import math
import threading
import time
import traceback
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from ..db import SessionLocal, engine
from ..models import Recipe

log = logging.getLogger("kucharka.feed")

# Bayesovský průměr: k hodnocení se přimyslí PRIOR_WEIGHT hlasů o hodnotě
# PRIOR_RATING. Málo hlasů tak skóre táhne k průměru místo do extrému.
PRIOR_RATING = 3.6
PRIOR_WEIGHT = 8.0

# Čerstvost: nově stažený recept dostane přirážku, která během pár měsíců
# doznívá. Drží úvodní stránku živou, aniž by přebila hodnocení.
RECENCY_BONUS = 0.6
RECENCY_HALFLIFE_DAYS = 45.0

# Srážky za vlastnosti, kvůli kterým recept na úvodní stránku nepatří.
PENALTY_NO_INGREDIENTS = 2.5   # ani jedna napárovaná surovina (typicky dekorace)
PENALTY_NO_INSTRUCTIONS = 2.0  # postup je prázdný nebo jen vycpávka
PENALTY_FEW_INGREDIENTS = 0.5  # jedna dvě suroviny – většinou to není recept
_SHORT_INSTR_CHARS = 120


def score(
    *,
    rating: float | None,
    rating_count: int | None,
    ing_total: int | None,
    instr_chars: int,
    created_at: datetime | None = None,
    now: datetime | None = None,
) -> float:
    """Skóre jednoho receptu. Čistá funkce – žádná DB, jde ji otestovat."""
    n = float(rating_count or 0)
    r = float(rating or 0.0)
    out = (r * n + PRIOR_RATING * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)

    if created_at is not None:
        now = now or datetime.now(timezone.utc)
        # created_at z DB bývá naivní; porovnáváme ve stejné rovině
        if created_at.tzinfo is None:
            now = now.replace(tzinfo=None)
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        out += RECENCY_BONUS * math.exp(-age_days / RECENCY_HALFLIFE_DAYS)

    total = ing_total or 0
    if total == 0:
        out -= PENALTY_NO_INGREDIENTS
    elif total < 3:
        out -= PENALTY_FEW_INGREDIENTS
    if instr_chars < _SHORT_INSTR_CHARS:
        out -= PENALTY_NO_INSTRUCTIONS
    return round(out, 4)


def score_for(recipe: Recipe, now: datetime | None = None) -> float:
    return score(
        rating=recipe.rating,
        rating_count=recipe.rating_count,
        ing_total=recipe.ing_total,
        instr_chars=len((recipe.instructions or "").strip()),
        created_at=recipe.created_at,
        now=now,
    )


# ─── Dávkový přepočet ────────────────────────────────────────────────────────

_lock = threading.Lock()
_state: dict = {
    "running": False, "done": 0, "total": 0, "updated": 0,
    "error": None, "started_at": None, "finished_at": None, "duration_s": None,
}

_CHUNK = 2000


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        return dict(_state)


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def _instr_len():
    """Počet ZNAKŮ oříznutého postupu, spočítaný databází.

    MariaDB má LENGTH v bajtech – česká diakritika by ho nafoukla a prahu
    `_SHORT_INSTR_CHARS` by se dotýkaly i delší postupy. CHAR_LENGTH počítá
    znaky, ale SQLite ho nezná; tam je znakové rovnou LENGTH.
    """
    fn = func.length if engine.dialect.name == "sqlite" else func.char_length
    return fn(func.trim(func.coalesce(Recipe.instructions, "")))


def recompute_all() -> dict:
    """Přepočítá `feed_score` u všech receptů.

    Čte jen sloupce, které skóre potřebuje (ne celé objekty), a zapisuje
    hromadně po dávkách – přes 171 tisíc receptů to jinak není únosné.
    Zápis jde přes hromadný ORM UPDATE podle primárního klíče: seznam dictů
    s `id` a novou hodnotou, jeden příkaz na dávku místo příkazu na řádek.

    Délka postupu se počítá NA SERVERU. Skóre z `instructions` potřebuje jen
    počet znaků, ale stáhnout kvůli tomu text všech receptů znamená stovky MB
    v paměti – na NASu zbytečné riziko. Takhle jde přes drát jedno číslo.
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Recipe.id, Recipe.rating, Recipe.rating_count, Recipe.ing_total,
                   _instr_len().label("instr_chars"), Recipe.created_at)
        ).all()
        _set(total=len(rows), done=0, updated=0, error=None)

        updates: list[dict] = []
        done = updated = 0
        for r in rows:
            updates.append({
                "id": r.id,
                "feed_score": score(
                    rating=r.rating, rating_count=r.rating_count,
                    ing_total=r.ing_total,
                    instr_chars=r.instr_chars or 0,
                    created_at=r.created_at, now=now,
                ),
            })
            done += 1
            if len(updates) >= _CHUNK:
                db.execute(update(Recipe), updates)
                db.commit()
                updated += len(updates)
                updates = []
                _set(done=done, updated=updated)
        if updates:
            db.execute(update(Recipe), updates)
            db.commit()
            updated += len(updates)

        duration = round(time.monotonic() - started, 1)
        _set(done=done, updated=updated, duration_s=duration, error=None)
        log.info("feed_score přepočítán u %s receptů za %s s", updated, duration)
        return {"updated": updated, "duration_s": duration}
    finally:
        db.close()


def recompute_all_async() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started_at=time.time(), finished_at=None,
                      error=None)

    def _worker():
        try:
            recompute_all()
        except Exception as exc:  # noqa: BLE001 – vlákno nesmí umřít potichu
            log.error("přepočet feed_score selhal: %s\n%s", exc, traceback.format_exc())
            _set(error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _set(running=False, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="feed-score").start()
    return True
