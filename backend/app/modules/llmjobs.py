"""Fronta odložených úloh na proxy (POST /mgmt/v1/jobs).

Proč: dávkové úlohy (párování surovin, kategorie, tagy, strom surovin) posílají
na GPU stovky dotazů. Dosud je appka serializovala vlastním zámkem
(`llmclient.ollama_gate`), jenže ten umí jen „jeden po druhém" – neví nic
o tom, jaký model je zrovna nahraný, a hlavně nedokáže pustit napřed
interaktivní dotaz (recept z fotky, OCR), který uživatel čeká teď hned.

Proxy tohle řeší za nás: úlohy seskupuje podle modelu, aby se model nahrával
co nejméně, a interaktivní dotazy mají přednost. Appka místo čekání na
odpověď pošle dávku a výsledky si vyzvedne.

Protokol (viz /mgmt/docs):
    POST   /mgmt/v1/jobs           {"jobs": [{"path": …, "body": …}], …}
                                   → 202 {"batch_id": …} nebo {"id": …}
    GET    /mgmt/v1/jobs?batch=…&bodies=1   → celá dávka i s výsledky
    GET    /mgmt/v1/jobs/{id}      → stav jedné úlohy, po dokončení `result`
    DELETE /mgmt/v1/jobs/{id}      → zruší čekající úlohu

POZOR: tvar odpovědi není v OpenAPI popsaný (schéma je prázdné), zdokumentovaná
jsou jen jména `id`, `batch_id`, `status` a `result`. Čtení je proto shovívavé
(viz `_job_state` a `_job_result`) a když se tvar nepotká, úloha se bere jako
neúspěšná a volající spadne na přímé volání – nikdy se netváří, že výsledek
má, když ho nemá.
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings

log = logging.getLogger("kucharka.llmjobs")

# Jak často se ptát na stav. Krátce na začátku (krátké dávky doběhnou hned),
# pak se interval prodlužuje, ať se fronta nezahltí dotazy na stav.
_POLL_START_S = 1.0
_POLL_MAX_S = 10.0
_POLL_GROWTH = 1.5

# Stavy, ve kterých je úloha doběhlá. Proxy je nedokumentuje, tak je bereme
# shovívavě – cokoli, co není čekání/běh, považujeme za konec.
_PENDING = {"queued", "pending", "waiting", "running", "in_progress", "new"}
_OK = {"done", "completed", "finished", "ok", "success", "succeeded"}


class JobsUnavailable(RuntimeError):
    """Fronta není použitelná (vypnutá, chybí klíč, proxy neodpovídá)."""


def enabled() -> bool:
    return bool(settings.llm_jobs_enabled and settings.llm_proxy_key
                and settings.ollama_url)


def _base() -> str:
    return f"{settings.ollama_url.rstrip('/')}/mgmt/v1/jobs"


def _headers() -> dict[str, str]:
    # /mgmt vyžaduje klíč vždycky, na rozdíl od inference.
    return {"Authorization": f"Bearer {settings.llm_proxy_key}"}


def submit(bodies: list[dict], *, path: str = "/api/chat",
           priority: int | None = None, timeout: float = 30) -> str:
    """Pošli dávku dotazů. Vrací identifikátor dávky pro `collect`."""
    if not enabled():
        raise JobsUnavailable("fronta úloh není zapnutá nebo chybí klíč proxy")
    if not bodies:
        raise ValueError("prázdná dávka")
    payload = {
        "jobs": [{"path": path, "body": b} for b in bodies],
        "priority": settings.llm_jobs_priority if priority is None else priority,
    }
    try:
        r = httpx.post(_base(), json=payload, headers=_headers(), timeout=timeout)
        r.raise_for_status()
        out = r.json()
    except Exception as exc:  # noqa: BLE001
        raise JobsUnavailable(f"odeslání dávky selhalo: {exc}") from exc

    batch = out.get("batch_id") or out.get("id")
    if batch is None:
        # Jednotlivé úlohy bez dávkového id – vezmeme si jejich id.
        ids = [j.get("id") for j in (out.get("jobs") or []) if j.get("id") is not None]
        if not ids:
            raise JobsUnavailable(f"odpověď fronty nemá id ani batch_id: {str(out)[:200]}")
        return ",".join(str(i) for i in ids)
    log.info("Fronta: odesláno %s úloh, dávka %s", len(bodies), batch)
    return str(batch)


def _job_state(job: dict) -> str:
    return str(job.get("status") or job.get("state") or "").lower()


def _job_result(job: dict) -> dict | None:
    res = job.get("result")
    if isinstance(res, dict):
        return res
    # Některé implementace vracejí odpověď zabalenou jinak.
    for key in ("response", "body", "output"):
        val = job.get(key)
        if isinstance(val, dict):
            return val
    return None


def _fetch(batch: str, *, timeout: float) -> list[dict]:
    """Aktuální stav celé dávky."""
    if "," in batch or batch.isdigit():
        # Dávka se nevrátila pod jedním id – doptáme se po jedné úloze.
        jobs = []
        for jid in batch.split(","):
            r = httpx.get(f"{_base()}/{jid}", headers=_headers(), timeout=timeout)
            r.raise_for_status()
            jobs.append(r.json())
        return jobs
    r = httpx.get(_base(), params={"batch": batch, "bodies": 1},
                  headers=_headers(), timeout=timeout)
    r.raise_for_status()
    out = r.json()
    return out if isinstance(out, list) else (out.get("items") or out.get("jobs") or [])


def collect(batch: str, *, count: int, wait_s: float,
            timeout: float = 30) -> list[dict | None]:
    """Počkej na dokončení dávky a vrať výsledky V POŘADÍ ODESLÁNÍ.

    Pořadí je zásadní: volající páruje výsledky zpátky na svoje vstupy podle
    indexu. Řadí se proto podle `id` úlohy, což je pořadí odeslání; kdyby id
    chybělo, vrací se pořadí, v jakém dorazilo, a nesrovnalost se hlásí.

    Úloha, která nedoběhla nebo skončila chybou, je `None` – volající s tím
    počítá stejně jako s neúspěšným přímým voláním.
    """
    deadline = time.monotonic() + wait_s
    interval = _POLL_START_S
    jobs: list[dict] = []
    while True:
        try:
            jobs = _fetch(batch, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise JobsUnavailable(f"dotaz na stav dávky selhal: {exc}") from exc

        pending = [j for j in jobs if _job_state(j) in _PENDING or not _job_state(j)]
        if jobs and not pending:
            break
        if time.monotonic() >= deadline:
            log.warning("Fronta: dávka %s nedoběhla do %s s (%s z %s hotovo)",
                        batch, wait_s, len(jobs) - len(pending), len(jobs) or count)
            break
        time.sleep(min(interval, max(0.1, deadline - time.monotonic())))
        interval = min(interval * _POLL_GROWTH, _POLL_MAX_S)

    if any(j.get("id") is not None for j in jobs):
        jobs.sort(key=lambda j: j.get("id") or 0)
    if len(jobs) != count:
        log.warning("Fronta: dávka %s vrátila %s úloh místo %s – výsledky se "
                    "nedají spolehlivě spárovat, beru je jako neúspěšné",
                    batch, len(jobs), count)
        return [None] * count

    out: list[dict | None] = []
    for j in jobs:
        state = _job_state(j)
        res = _job_result(j)
        if res is None or (state and state not in _OK and state not in _PENDING):
            if state not in _PENDING:
                log.info("Fronta: úloha %s skončila jako %r bez výsledku",
                         j.get("id"), state or "?")
            out.append(None)
        else:
            out.append(res)
    return out


def cancel(batch: str, *, timeout: float = 15) -> int:
    """Zruš čekající úlohy dávky. Vrací, kolik jich šlo zrušit."""
    if not enabled():
        return 0
    try:
        jobs = _fetch(batch, timeout=timeout)
    except Exception:  # noqa: BLE001
        return 0
    done = 0
    for j in jobs:
        jid = j.get("id")
        if jid is None or _job_state(j) not in _PENDING:
            continue
        try:
            httpx.delete(f"{_base()}/{jid}", headers=_headers(), timeout=timeout)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.info("Fronta: úlohu %s se nepodařilo zrušit: %s", jid, exc)
    return done


def health() -> dict:
    """Je fronta dosažitelná? Pro kartu v administraci."""
    if not settings.llm_proxy_key:
        return {"ok": False, "error": "Chybí klíč proxy (Administrace → Nástroje)."}
    if not settings.ollama_url:
        return {"ok": False, "error": "Není nastavená adresa proxy (OLLAMA_URL)."}
    try:
        r = httpx.get(_base(), params={"limit": 1}, headers=_headers(), timeout=10)
        r.raise_for_status()
        return {"ok": True, "enabled": bool(settings.llm_jobs_enabled)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
