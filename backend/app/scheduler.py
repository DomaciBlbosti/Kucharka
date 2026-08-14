"""Plánovač úloh na pozadí (crawler, překlad, párování).

Úlohy běží přes jediný worker, takže se nepřekrývají a nervou si GPU.
Vše lze přenastavit za běhu z administrace.
"""
from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger("kucharka.scheduler")

_sched = None


def _get_sched():
    global _sched
    if _sched is None:
        try:
            from apscheduler.executors.pool import ThreadPoolExecutor
            from apscheduler.schedulers.background import BackgroundScheduler

            # jediný worker → úlohy na pozadí se nepřekrývají (šetří GPU)
            _sched = BackgroundScheduler(
                daemon=True, executors={"default": ThreadPoolExecutor(1)}
            )
            _sched.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Plánovač nelze spustit: %s", exc)
            return None
    return _sched


def _reschedule(job_id: str, enabled: bool, minutes: int, func) -> None:
    sched = _get_sched()
    if sched is None:
        return
    try:
        sched.remove_job(job_id)
    except Exception:  # noqa: BLE001
        pass
    if not enabled:
        log.info("Úloha '%s' vypnuta.", job_id)
        return
    sched.add_job(
        func, "interval", minutes=max(1, minutes), id=job_id,
        max_instances=1, coalesce=True,
    )
    log.info("Úloha '%s' naplánována každých %s min.", job_id, minutes)


def _run_crawler():
    """Stáhne přírůstek receptů a ROVNOU ho protáhne celým zpracováním
    (párování → kategorie → tagy) – jeden cyklus = kompletně hotové recepty,
    žádné čekání na další tik zpracovací úlohy. Zpracování má vlastní
    is_running pojistky, dvojí běh nehrozí (plánovač je stejně jednovláknový)."""
    from .modules import crawler

    result = crawler.crawl_sites(max_recipes=settings.crawler_max_per_run)
    if (result or {}).get("added"):
        _run_match()


def _run_translate():
    from .modules import translate

    if translate.status().get("running"):
        return
    translate.retranslate_all()


def _run_match():
    """Automatické zpracování surovin a tagů – kompletní kolečko pro nově
    stažené recepty, ať se nic nekupí na ruční spouštění:
      1. slovník + fuzzy (backfill),
      2. dávkové LLM dopárování + kontext receptu (llm_match),
      3. kategorizace nových surovin (jen nezařazené),
      4. otagování nových receptů (jen neotagované).
    Kroky 3–4 jsou při prázdné frontě no-op. Vše synchronně v jednom
    workeru, ať se úlohy nepřekrývají a nervou si GPU."""
    from .modules import backfill, categorize, llm_match, llmclient, tagging

    if backfill.is_running():
        return
    backfill.backfill()
    if settings.llm_match_enabled and not llm_match.is_running():
        llm_match.process_batch()
    if llmclient.is_available():
        if not categorize.is_running():
            categorize.categorize_all(only_missing=True)
        if not tagging.is_running():
            tagging.tag_all(only_missing=True)


def _run_lidl_sync():
    from .db import SessionLocal
    from .modules import lidl_import

    db = SessionLocal()
    try:
        lidl_import.sync_all(db)
    finally:
        db.close()


def configure_crawler() -> None:
    _reschedule("crawler", settings.crawler_enabled, settings.crawler_interval_min, _run_crawler)


def configure_translate() -> None:
    _reschedule(
        "translate", settings.auto_translate_enabled,
        settings.auto_translate_interval_min, _run_translate,
    )


def configure_match() -> None:
    _reschedule(
        "match", settings.auto_match_enabled,
        settings.auto_match_interval_min, _run_match,
    )


def configure_lidl() -> None:
    _reschedule(
        "lidl_sync", settings.lidl_sync_enabled,
        settings.lidl_sync_interval_min, _run_lidl_sync,
    )


def configure_all() -> None:
    configure_crawler()
    configure_translate()
    configure_match()
    configure_lidl()


# Mapa job_id → (lidský název, jak zjistit, jestli zrovna běží). Funkce pro
# "běží" jsou lazy (import uvnitř), ať se modul nenačítá zbytečně brzy.
def _crawler_running() -> bool:
    from .modules import crawler

    return crawler.is_running()


def _translate_running() -> bool:
    from .modules import translate

    return translate.is_running()


def _match_running() -> bool:
    from .modules import backfill, categorize, llm_match, tagging

    return (
        backfill.is_running() or llm_match.is_running()
        or categorize.is_running() or tagging.is_running()
    )


_JOB_META = {
    "crawler": ("Objevování receptů (crawler)", _crawler_running),
    "translate": ("Automatický překlad", _translate_running),
    "match": ("Zpracování surovin (párování + kategorie + tagy)", _match_running),
    "lidl_sync": ("Synchronizace Lidl účtenek", None),
}


def jobs_overview() -> dict:
    """Přehled úloh na pozadí pro admin: jestli plánovač běží, které úlohy
    jsou naplánované, kdy poběží příště a jestli zrovna něco běží."""
    sched = _sched
    scheduler_running = bool(sched and getattr(sched, "running", False))

    scheduled = {}
    if sched is not None:
        for job in sched.get_jobs():
            nxt = getattr(job, "next_run_time", None)
            scheduled[job.id] = nxt.isoformat() if nxt else None

    jobs = []
    for job_id, (label, running_fn) in _JOB_META.items():
        try:
            running = bool(running_fn()) if running_fn else False
        except Exception:  # noqa: BLE001 - stav se nesmí shodit na přehledu
            running = False
        jobs.append(
            {
                "id": job_id,
                "label": label,
                "scheduled": job_id in scheduled,
                "next_run": scheduled.get(job_id),
                "running": running,
            }
        )

    return {"scheduler_running": scheduler_running, "jobs": jobs}
