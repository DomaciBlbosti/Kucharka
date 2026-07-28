"""Přihlášení a stav zabezpečení.

Token se klientovi předává dvěma cestami zároveň:
  * v těle odpovědi (frontend si ho drží v localStorage a posílá v
    Authorization hlavičce – historické chování, funguje dál),
  * jako HttpOnly cookie s dlouhou platností („zůstat přihlášen") – přežije
    smazání localStorage, funguje pro přímé odkazy (exporty, /uploads) a
    JS se k ní nedostane (XSS nemá co ukrást).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth
from ..config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "kucharka_auth"
TOKEN_DAYS = 90  # „zapamatovat si mě" – domácí appka, dlouhá platnost je záměr


class LoginRequest(BaseModel):
    password: str


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
    return {
        "required": settings.auth_enabled,
        "authenticated": (not settings.auth_enabled)
        or auth.valid_token(token_from_request(request)),
    }


@router.post("/login")
def login(req: LoginRequest, response: Response):
    if not settings.auth_enabled:
        return {"ok": True, "token": "", "required": False}
    if not auth.verify_password(req.password):
        raise HTTPException(401, "Špatné heslo.")
    token = auth.make_token(days=TOKEN_DAYS)
    _set_cookie(response, token)
    return {"ok": True, "token": token, "required": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
