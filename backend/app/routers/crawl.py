"""API pro autonomní crawler."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import CrawlUrl, Recipe
from ..modules import crawler

router = APIRouter(prefix="/api/crawl", tags=["crawler"])


class CrawlRequest(BaseModel):
    mode: str = "sites"  # "sites" = procházet weby (default), "query" = přes SearXNG
    domains: list[str] | None = None
    queries: list[str] | None = None
    max_recipes: int = 30
    per_site: int = 12
    per_query: int = 8


@router.get("/status")
def crawl_status():
    s = crawler.status()
    s["scheduler_enabled"] = settings.crawler_enabled
    s["auto_ingredients"] = settings.auto_ingredients
    s["domains_count"] = len(settings.recipe_domains)
    return s


@router.post("/run")
def crawl_run(req: CrawlRequest):
    if req.mode == "query":
        if not settings.searxng_enabled:
            return {"started": False, "reason": "SearXNG není nastavený (SEARXNG_URL)."}
        started = crawler.crawl_async(
            queries=req.queries,
            max_recipes=req.max_recipes,
            per_query=req.per_query,
        )
    else:  # sites – procházení webů přes sitemapy (nepotřebuje SearXNG)
        started = crawler.crawl_sites_async(
            domains=req.domains,
            max_recipes=req.max_recipes,
            per_site=req.per_site,
        )
    return {"started": started, "status": crawler.status()}


@router.get("/queue/stats")
def queue_stats():
    return crawler.queue_stats()


@router.get("/queue")
def queue_list(
    status: str | None = Query(None, description="pending|ok|skip|error, prázdné = vše"),
    domain: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    filters = []
    if status:
        filters.append(CrawlUrl.status == status)
    if domain:
        filters.append(CrawlUrl.domain == domain)

    count_q = select(func.count(CrawlUrl.id))
    for f in filters:
        count_q = count_q.where(f)
    total = db.scalar(count_q) or 0

    q = select(CrawlUrl).order_by(CrawlUrl.discovered_at.desc())
    for f in filters:
        q = q.where(f)
    rows = db.scalars(q.offset(offset).limit(limit)).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "domain": r.domain,
                "url": r.url,
                "status": r.status,
                "error": r.error,
                "recipe_id": r.recipe_id,
                "attempts": r.attempts,
                "discovered_at": r.discovered_at.isoformat() if r.discovered_at else None,
                "attempted_at": r.attempted_at.isoformat() if r.attempted_at else None,
            }
            for r in rows
        ],
    }


class PruneRequest(BaseModel):
    domain: str | None = None  # prázdné = všechny nakonfigurované domény
    dry_run: bool = True  # jen spočítat, nemazat (bezpečné pro první náhled)


@router.post("/queue/prune")
def prune_queue(req: PruneRequest):
    """Spusť na POZADÍ pročištění fronty od 'pending' URL, které se už
    NEVYSKYTUJÍ v aktuální (deterministické) sitemapě dané domény. To
    odstraní přebytek, který dřív nabobtnal kvůli náhodnému vzorkování
    sitemap (fronta byla klidně 4–6× větší než skutečný web).

    Běží asynchronně, protože stažení a parsování celých sitemap všech domén
    trvá minuty (a HTTP request by spadl na Cloudflare 524 timeoutu). Stav
    sleduj přes GET /api/crawl/queue/prune-status.

    Maže se JEN status 'pending' – hotové ('ok'), přeskočené ('skip') i
    chybné ('error') záznamy zůstávají (historie výsledků se nemaže).

    `dry_run=True` (výchozí) jen spočítá, kolik by se smazalo, nic nemění.
    """
    domains = [req.domain] if req.domain else (list(settings.recipe_domains) or crawler.DEFAULT_SITES)
    started = crawler.prune_async(domains, dry_run=req.dry_run)
    return {"started": started, "status": crawler.prune_status()}


@router.get("/queue/prune-status")
def prune_status():
    """Stav běžícího/dokončeného pročištění fronty."""
    return crawler.prune_status()


class RetryRequest(BaseModel):
    domain: str | None = None  # prázdné = napříč všemi doménami


@router.post("/queue/retry-errors")
def retry_errors(req: RetryRequest, db: Session = Depends(get_db)):
    """Přeřaď URL se stavem 'error' zpět na 'pending', aby je crawler zkusil
    znovu. Užitečné po opravě chyby, kvůli které dřív padaly. Vrací počet
    přeřazených URL. Nespouští crawler – jen připraví frontu."""
    q = select(CrawlUrl).where(CrawlUrl.status == "error")
    if req.domain:
        q = q.where(CrawlUrl.domain == req.domain)
    rows = db.scalars(q).all()
    for r in rows:
        r.status = "pending"
        r.error = None
    db.commit()
    return {"requeued": len(rows), "domain": req.domain}


class ResyncRequest(BaseModel):
    domains: list[str] | None = None  # prázdné = všechny nakonfigurované


@router.post("/resync")
def resync(req: ResyncRequest):
    """Vynuť okamžité znovunačtení sitemap (obejde 6h okno) a doplň nové URL
    do fronty. Neprochází je – jen aktualizuje mapu. Vrací počet nově
    přidaných URL po doménách."""
    domains = req.domains or list(settings.recipe_domains) or crawler.DEFAULT_SITES
    result: dict[str, int] = {}
    db = SessionLocal()
    try:
        for dom in domains:
            try:
                result[dom] = crawler.sync_domain(db, dom, force=True)
            except Exception as exc:  # noqa: BLE001
                result[dom] = -1  # -1 = sync selhal (viz server log)
    finally:
        db.close()
    return {"resynced": result, "queue": crawler.queue_stats()}


@router.get("/queue/export")
def queue_export(
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    status: str | None = None,
    domain: str | None = None,
    db: Session = Depends(get_db),
):
    """Stáhni celou mapu prohledaných odkazů (bez limitu) – URL, stav,
    výsledek, chyba a odkaz na získaný recept. `fmt=csv` (výchozí) nebo
    `fmt=json`."""
    q = (
        select(CrawlUrl, Recipe.title, Recipe.source_url)
        .outerjoin(Recipe, CrawlUrl.recipe_id == Recipe.id)
        .order_by(CrawlUrl.domain.asc(), CrawlUrl.discovered_at.asc())
    )
    if status:
        q = q.where(CrawlUrl.status == status)
    if domain:
        q = q.where(CrawlUrl.domain == domain)
    rows = db.execute(q).all()

    def recipe_url(cu: CrawlUrl) -> str:
        return f"/recept/{cu.recipe_id}" if cu.recipe_id else ""

    if fmt == "json":
        payload = [
            {
                "domain": cu.domain,
                "url": cu.url,
                "status": cu.status,
                "error": cu.error,
                "attempts": cu.attempts,
                "recipe_id": cu.recipe_id,
                "recipe_title": title,
                "recipe_path": recipe_url(cu),
                "discovered_at": cu.discovered_at.isoformat() if cu.discovered_at else None,
                "attempted_at": cu.attempted_at.isoformat() if cu.attempted_at else None,
            }
            for cu, title, _src in rows
        ]
        import json as _json

        buf = io.BytesIO(_json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        return StreamingResponse(
            buf,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=sitemap.json"},
        )

    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow([
        "domain", "url", "status", "error", "attempts",
        "recipe_id", "recipe_title", "recipe_path",
        "discovered_at", "attempted_at",
    ])
    for cu, title, _src in rows:
        w.writerow([
            cu.domain, cu.url, cu.status, cu.error or "", cu.attempts,
            cu.recipe_id or "", title or "", recipe_url(cu),
            cu.discovered_at.isoformat() if cu.discovered_at else "",
            cu.attempted_at.isoformat() if cu.attempted_at else "",
        ])
    return StreamingResponse(
        io.BytesIO(out.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sitemap.csv"},
    )
