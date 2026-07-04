"""Self-update z Gitu přes WEB UI (po vzoru ERI).

Pod Dockerem běží uvicorn v supervisor smyčce (entrypoint.sh). „Aktualizovat"
jen ukončí proces; smyčka udělá `git pull`, rebuild frontendu a restart. Mimo
Docker (WSL) endpoint provede `git pull` a vyzve k ručnímu restartu.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ..config import settings

router = APIRouter(prefix="/api/system", tags=["system"])


def _repo_dir() -> str:
    if settings.repo_dir:
        return settings.repo_dir
    # .../backend/app/routers/system.py → kořen repa = parents[3]
    return str(Path(__file__).resolve().parents[3])


def _git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", _repo_dir(), *args],
            capture_output=True, text=True, timeout=120,
        )
        return (r.stdout or r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return f"chyba: {exc}"


def _branch() -> str:
    b = _git("rev-parse", "--abbrev-ref", "HEAD")
    return b if b and "chyba" not in b else "main"


@router.get("/jobs")
def jobs():
    """Přehled úloh na pozadí: běží plánovač? které úlohy jsou naplánované,
    kdy poběží příště, a jestli zrovna něco běží."""
    from .. import scheduler

    return scheduler.jobs_overview()


@router.get("/log")
def system_log(
    limit: int = Query(200, le=1000),
    level: str | None = Query(None, description="INFO|WARNING|ERROR – filtr úrovně"),
    logger: str | None = Query(None, description="prefix jména loggeru, např. 'kucharka.crawler'"),
    contains: str | None = Query(None, description="podřetězec ve zprávě"),
):
    """Posledních N log záznamů s časovými razítky (z paměti appky). Slouží
    k ověření, že se úlohy na pozadí opravdu spouští, bez přístupu k
    `docker logs`. Nejnovější první."""
    from ..modules import logbuffer

    return {"items": logbuffer.get_records(limit=limit, level=level, logger_prefix=logger, contains=contains)}


@router.get("/version")
def version():
    return {
        "enabled": settings.update_enabled,
        "supervised": os.environ.get("SUPERVISED") == "1",
        "commit": _git("log", "-1", "--format=%h"),
        "date": _git("log", "-1", "--format=%ci"),
        "subject": _git("log", "-1", "--format=%s"),
        "branch": _branch(),
    }


@router.post("/check")
def check():
    if not settings.update_enabled:
        raise HTTPException(403, "Aktualizace přes UI nejsou povolené (UPDATE_ENABLED).")
    _git("fetch", "--quiet")
    branch = _branch()
    behind = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    try:
        n = int(behind)
    except ValueError:
        n = 0
    return {
        "behind": n,
        "update_available": n > 0,
        "remote_subject": _git("log", "-1", "--format=%s", f"origin/{branch}"),
        "branch": branch,
    }


@router.post("/update")
def update():
    if not settings.update_enabled:
        raise HTTPException(403, "Aktualizace přes UI nejsou povolené (UPDATE_ENABLED).")
    supervised = os.environ.get("SUPERVISED") == "1"
    if supervised:
        # supervisor smyčka po ukončení procesu udělá pull + rebuild + restart
        Path(_repo_dir(), ".needs-build").touch()

        def _restart():
            time.sleep(0.6)
            os._exit(0)

        threading.Thread(target=_restart, daemon=True).start()
        return {"mode": "docker", "message": "Stahuji z Gitu a restartuji…"}
    # mimo Docker: stáhni a vyzvi k ručnímu restartu
    out = _git("pull", "--ff-only")
    return {"mode": "manual", "output": out,
            "message": "Staženo. Restartuj API pro aplikaci změn."}
