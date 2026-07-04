"""In-memory kruhový buffer posledních log záznamů.

Umožňuje číst posledních N log řádků přímo přes API (Admin → Log), aniž by
byl potřeba přístup k `docker logs`. Handler se připojí na root logger při
startu appky; drží jen omezený počet záznamů (paměť), starší se zahazují.

Není to náhrada za `docker logs` (ten má kompletní historii) – jde o rychlý
náhled "co se právě dělo", hlavně kvůli ověření, že se úlohy na pozadí
opravdu spouští.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone

_MAX = 2000  # kolik posledních záznamů držet v paměti

_lock = threading.Lock()
_records: deque[dict] = deque(maxlen=_MAX)


class RingBufferHandler(logging.Handler):
    """Log handler, který si drží posledních _MAX záznamů v paměti."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - formátování zprávy nesmí shodit logging
            msg = str(record.msg)
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
        }
        with _lock:
            _records.append(entry)


_installed = False


def install(level: int = logging.INFO) -> None:
    """Připoj ring buffer handler na root logger (idempotentní)."""
    global _installed
    if _installed:
        return
    handler = RingBufferHandler()
    handler.setLevel(level)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    _installed = True


def get_records(
    limit: int = 200,
    level: str | None = None,
    logger_prefix: str | None = None,
    contains: str | None = None,
) -> list[dict]:
    """Vrať posledních `limit` záznamů (nejnovější první), volitelně filtr
    podle úrovně, prefixu loggeru a podřetězce ve zprávě."""
    with _lock:
        items = list(_records)

    if level:
        want = level.upper()
        items = [r for r in items if r["level"] == want]
    if logger_prefix:
        items = [r for r in items if r["logger"].startswith(logger_prefix)]
    if contains:
        needle = contains.lower()
        items = [r for r in items if needle in r["message"].lower()]

    return items[-limit:][::-1]  # nejnovější první
