"""Přihlášení a stav zabezpečení.

Token se klientovi předává dvěma cestami zároveň:
  * v těle odpovědi (frontend si ho drží v localStorage a posílá v
    Authorization hlavičce – historické chování, funguje dál),
  * jako HttpOnly cookie s dlouhou platností („zůstat přihlášen") – přežije
    smazání localStorage, funguje pro přímé odkazy (exporty, /uploads) a
    JS se k ní nedostane (XSS nemá co ukrást).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..config import settings
from ..db import get_db
from ..models import AppUser

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "kucharka_auth"
TOKEN_DAYS = 90  # „zapamatovat si mě" – domácí appka, dlouhá platnost je záměr


class LoginRequest(BaseModel):
    password: str
    username: str | None = None  # prázdné = sdílené heslo (záložní cesta)


def token_from_request(request: Request) -> str | None:
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    return request.query_params.get("token")


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_DAYS * 86400,
        httponly=True,     # JS na token nevidí
        samesite="lax",    # posílá se při navigaci, ne z cizích stránek
        secure=False,      # appka běží po LAN na http; za https proxy cookie funguje taky
        path="/",
    )


@router.get("/status")
def status(request: Request):
    info = auth.token_info(token_from_request(request))
    return {
        "required": settings.auth_enabled,
        "authenticated": (not settings.auth_enabled) or info is not None,
        # Frontend podle tohohle ukáže políčko pro jméno a schová Admin záložku.
        "users": bool(settings.auth_users),
        "shared_password": bool(settings.auth_password_hash),
        "me": info if settings.auth_enabled else {"uid": None, "name": "", "role": "admin"},
    }


@router.post("/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    if not settings.auth_enabled:
        return {"ok": True, "token": "", "required": False}
    name = (req.username or "").strip()
    if name:
        user = db.scalar(select(AppUser).where(AppUser.username == name))
        if user is None or not user.active or not auth.check_password(req.password, user.password_hash):
            raise HTTPException(401, "Špatné jméno nebo heslo.")
        user.last_login_at = datetime.utcnow()
        db.commit()
        token = auth.make_token(days=TOKEN_DAYS, user=user)
    else:
        if not settings.auth_password_hash:
            raise HTTPException(401, "Zadej uživatelské jméno.")
        if not auth.verify_password(req.password):
            raise HTTPException(401, "Špatné heslo.")
        token = auth.make_token(days=TOKEN_DAYS)
    _set_cookie(response, token)
    return {"ok": True, "token": token, "required": True, "me": auth.token_info(token)}


@router.get("/me")
def me(request: Request):
    return auth.token_info(token_from_request(request)) or {"uid": None, "name": "", "role": "admin"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
