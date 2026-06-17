"""Plánovač crawleru, který lze přenastavit za běhu (z administrace)."""
from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger("kucharka.scheduler")

_sched = None


def _get_sched():
    global _sched
    if _sched is None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            _sched = BackgroundScheduler(daemon=True)
            _sched.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Plánovač nelze spustit: %s", exc)
            return None
    return _sched


def configure_crawler() -> None:
    """Nastav/zruš periodický crawl podle aktuálního nastavení."""
    sched = _get_sched()
    if sched is None:
        return
    try:
        sched.remove_job("crawler")
    except Exception:  # noqa: BLE001
        pass
    if not settings.crawler_enabled:
        log.info("Crawler na pozadí vypnut.")
        return
    from .modules import crawler

    sched.add_job(
        lambda: crawler.crawl_sites(max_recipes=settings.crawler_max_per_run),
        "interval",
        minutes=max(1, settings.crawler_interval_min),
        id="crawler",
        max_instances=1,
        coalesce=True,
    )
    log.info(
        "Crawler naplánován každých %s min (max %s receptů/běh).",
        settings.crawler_interval_min,
        settings.crawler_max_per_run,
    )
