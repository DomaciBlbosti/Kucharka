"""Přihlášení a stav zabezpečení."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth
from ..config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


def token_from_request(request: Request) -> str | None:
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return request.query_params.get("token")


@router.get("/status")
def status(request: Request):
    return {
        "required": settings.auth_enabled,
        "authenticated": (not settings.auth_enabled)
        or auth.valid_token(token_from_request(request)),
    }


@router.post("/login")
def login(req: LoginRequest):
    if not settings.auth_enabled:
        return {"ok": True, "token": "", "required": False}
    if not auth.verify_password(req.password):
        raise HTTPException(401, "Špatné heslo.")
    return {"ok": True, "token": auth.make_token(), "required": True}
