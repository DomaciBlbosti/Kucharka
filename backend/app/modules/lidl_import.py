"""Import nákupů z Lidl Plus účtenek do spíže ("mám doma").

Přihlášení (2FA/OAuth) vyžaduje reálný prohlížeč – to v appce nemáme a
nechceme kvůli tomu tahat Chromium do lightweight Docker image. Proto se
`refresh_token` získává JEDNORÁZOVĚ mimo appku (na PC, přes CLI nástroj
`lidl-plus auth` z balíčku `lidl-plus`) a do Kuchařky se jen vloží – od
tohodle bodu je veškerá komunikace čisté REST/JSON (obnovení tokenu, seznam
účtenek, detail účtenky), přesně jako zbytek appky.

Víc účtů (např. Aleš + manželka) je běžný případ – každý účet je vlastní
řádek v `lidl_account`, sync běží pro všechny nezávisle.

Použitá knihovna `lidlplus` (PyPI `lidl-plus`) importuje Selenium/browser
věci jen v try/except ImportError bloku a `.tickets()`/`.ticket()` je čisté
`requests` volání – takže nepotřebujeme browser ani při běhu v Dockeru, jen
`pip install lidl-plus` (bez `[auth]` extra).

Nejistota: přesný název klíče s položkami uvnitř odpovědi `.ticket(id)`
nemám z dokumentace knihovny stoprocentně potvrzený (dokumentace ukazuje jen
tvar JEDNÉ položky, ne obalový klíč). `_extract_item_names` proto zkouší víc
kandidátů a při neúspěchu zaloguje, jaké klíče ticket ve skutečnosti má – ať
se to podle prvního reálného běhu snadno dopilovat.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LidlAccount, LidlReceipt
from .normalizer import match_ingredient

log = logging.getLogger("kucharka.lidl_import")

_ITEM_LIST_KEYS = ("itemsLine", "lines", "items", "articles", "products")
_DATE_KEYS = ("date", "purchaseDate", "createdAt", "issueDate")


def _client(account: LidlAccount):
    from lidlplus import LidlPlusApi  # import až tady, ať appka jede i bez balíčku

    return LidlPlusApi(
        language=account.language or "cs",
        country=account.country or "CZ",
        refresh_token=account.refresh_token,
    )


def test_connection(country: str, language: str, refresh_token: str) -> dict:
    """Ověř, že token funguje – zavolá se při přidávání účtu v adminu."""
    from lidlplus import LidlPlusApi

    client = LidlPlusApi(language=language or "cs", country=country or "CZ", refresh_token=refresh_token)
    tickets = client.tickets()
    return {"ok": True, "tickets_found": len(tickets), "new_refresh_token": client.refresh_token}


def _extract_item_names(ticket: dict) -> list[str]:
    for key in _ITEM_LIST_KEYS:
        lines = ticket.get(key)
        if isinstance(lines, list) and lines:
            names = [str(l.get("name", "")).strip() for l in lines if isinstance(l, dict)]
            names = [n for n in names if n]
            if names:
                return names
    log.warning(
        "Lidl účtenka: nenašel jsem seznam položek v žádném z klíčů %s. "
        "Skutečné klíče v ticketu: %s",
        _ITEM_LIST_KEYS, sorted(ticket.keys()),
    )
    return []


def _ticket_date(ticket: dict) -> str | None:
    for key in _DATE_KEYS:
        v = ticket.get(key)
        if v:
            return str(v)
    return None


def sync_account(db: Session, account: LidlAccount) -> dict:
    """Stáhni nové účtenky daného účtu a konfidentně napárované položky
    rovnou přidej do spíže. Nejednoznačné (bez shody v DB surovin) položky
    NEpřidávám automaticky – jen se sečtou do `items_unmatched`, ať se
    appka sama neplní odhady. Ruční doplnění zůstává přes běžný
    "Recept z fotky"/"Účtenka" review, nebo se dá párování dohnat později."""
    try:
        client = _client(account)
        tickets = client.tickets()
    except Exception as exc:  # noqa: BLE001
        log.warning("Lidl sync (%s) selhal při stahování účtenek: %s", account.label, exc)
        account.last_sync_error = str(exc)[:2000]
        db.commit()
        return {"ok": False, "error": str(exc)}

    known_ids = {
        r.ticket_id
        for r in db.scalars(
            select(LidlReceipt).where(LidlReceipt.account_id == account.id)
        )
    }
    new_tickets = [t for t in tickets if str(t.get("id")) not in known_ids]

    matched_total = 0
    unmatched_total = 0
    tickets_processed = 0

    for t in new_tickets:
        ticket_id = str(t.get("id"))
        try:
            detail = client.ticket(ticket_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Lidl sync (%s): detail účtenky %s selhal: %s", account.label, ticket_id, exc)
            continue

        names = _extract_item_names(detail)
        matched_here = 0
        unmatched_here = 0
        for name in names:
            ing = match_ingredient(db, name)
            if ing is None:
                unmatched_here += 1
                continue
            from ..models import PantryItem

            existing = db.scalar(select(PantryItem).where(PantryItem.ingredient_id == ing.id))
            if existing is None:
                db.add(PantryItem(ingredient_id=ing.id))
            matched_here += 1

        db.add(LidlReceipt(
            account_id=account.id,
            ticket_id=ticket_id,
            purchased_at=_ticket_date(t) or _ticket_date(detail),
            items_matched=matched_here,
            items_unmatched=unmatched_here,
        ))
        matched_total += matched_here
        unmatched_total += unmatched_here
        tickets_processed += 1

    account.last_sync_at = datetime.utcnow()
    account.last_sync_error = None
    # nový refresh_token (Lidl ho při obnovení rotuje) – ulož, ať zůstane platný
    if client.refresh_token and client.refresh_token != account.refresh_token:
        account.refresh_token = client.refresh_token
    db.commit()

    log.info(
        "Lidl sync (%s): %s nových účtenek, %s položek napárováno, %s bez shody.",
        account.label, tickets_processed, matched_total, unmatched_total,
    )
    return {
        "ok": True,
        "tickets_total": len(tickets),
        "tickets_new": tickets_processed,
        "items_matched": matched_total,
        "items_unmatched": unmatched_total,
    }


def sync_all(db: Session) -> list[dict]:
    accounts = db.scalars(select(LidlAccount).where(LidlAccount.enabled.is_(True))).all()
    return [{"account": a.label, **sync_account(db, a)} for a in accounts]
