"""Správa napojených Lidl Plus účtů a import účtenek do spíže.

Přihlašovací `refresh_token` se získává mimo appku (viz modules/lidl_import.py
docstring) – sem se jen vkládá a appka ho pak sama průběžně obnovuje.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import LidlAccount, LidlReceipt
from ..modules import lidl_import

log = logging.getLogger("kucharka.lidl")
router = APIRouter(prefix="/api/lidl", tags=["lidl"])


def _account_out(a: LidlAccount) -> dict:
    # refresh_token se NIKDY nevrací ven, i když je to home appka za heslem
    return {
        "id": a.id,
        "label": a.label,
        "country": a.country,
        "language": a.language,
        "enabled": a.enabled,
        "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
        "last_sync_error": a.last_sync_error,
    }


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.scalars(select(LidlAccount).order_by(LidlAccount.id)).all()
    return [_account_out(a) for a in accounts]


class AccountCreate(BaseModel):
    label: str
    country: str = "CZ"
    language: str = "cs"
    refresh_token: str


@router.post("/accounts")
def add_account(req: AccountCreate, db: Session = Depends(get_db)):
    label = req.label.strip()
    token = req.refresh_token.strip()
    if not label or not token:
        raise HTTPException(400, "Vyplň popisek účtu a refresh token.")
    try:
        check = lidl_import.test_connection(req.country, req.language, token)
    except ImportError:
        raise HTTPException(
            500,
            "Balíček 'lidl-plus' není nainstalovaný (přidej do requirements.txt a aktualizuj appku).",
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Token nefunguje: {exc}") from None

    acc = LidlAccount(
        label=label,
        country=req.country.upper().strip() or "CZ",
        language=req.language.lower().strip() or "cs",
        refresh_token=check.get("new_refresh_token") or token,
    )
    db.add(acc)
    db.commit()
    return {"account": _account_out(acc), "tickets_found": check["tickets_found"]}


class AccountUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None


@router.put("/accounts/{account_id}")
def update_account(account_id: int, req: AccountUpdate, db: Session = Depends(get_db)):
    acc = db.get(LidlAccount, account_id)
    if acc is None:
        raise HTTPException(404, "Účet nenalezen.")
    if req.label is not None and req.label.strip():
        acc.label = req.label.strip()
    if req.enabled is not None:
        acc.enabled = req.enabled
    db.commit()
    return _account_out(acc)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(LidlAccount, account_id)
    if acc is None:
        raise HTTPException(404, "Účet nenalezen.")
    db.delete(acc)
    db.commit()
    return {"deleted": True}


@router.post("/accounts/{account_id}/sync")
def sync_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(LidlAccount, account_id)
    if acc is None:
        raise HTTPException(404, "Účet nenalezen.")
    result = lidl_import.sync_account(db, acc)
    if not result.get("ok"):
        raise HTTPException(502, f"Sync selhal: {result.get('error')}")
    return result


@router.post("/sync-all")
def sync_all(db: Session = Depends(get_db)):
    return {"results": lidl_import.sync_all(db)}


@router.get("/accounts/{account_id}/receipts")
def list_receipts(account_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(LidlReceipt)
        .where(LidlReceipt.account_id == account_id)
        .order_by(LidlReceipt.imported_at.desc())
        .limit(50)
    ).all()
    return [
        {
            "ticket_id": r.ticket_id,
            "purchased_at": r.purchased_at,
            "items_matched": r.items_matched,
            "items_unmatched": r.items_unmatched,
            "imported_at": r.imported_at.isoformat(),
        }
        for r in rows
    ]
