"""Volitelné zabezpečení heslem.

Jedno sdílené heslo (hash v app_setting). Po přihlášení dostane klient
podepsaný token (HMAC + expirace), který posílá v hlavičce Authorization.
Žádná externí závislost – hashlib/hmac/secrets. Stav je v paměti (settings),
takže ověření tokenu na každém requestu nesahá do DB.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from .config import settings
from .db import SessionLocal
from .models import AppSetting, AppUser

log = logging.getLogger("kucharka.auth")

_PW_KEY = "app_password_hash"
_SECRET_KEY = "auth_secret"
_ITER = 200_000


def _get(db, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def _put(db, key: str, val: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = val
    else:
        db.add(AppSetting(key=key, value=val))


def load(db) -> None:
    """Načti stav do settings při startu."""
    settings.auth_password_hash = _get(db, _PW_KEY)
    refresh_users(db)
    sec = _get(db, _SECRET_KEY)
    if not sec:
        sec = secrets.token_hex(32)
        _put(db, _SECRET_KEY, sec)
        db.commit()
    settings.auth_secret = sec


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER).hex()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + _hash(password, salt)


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        salt_hex, h_hex = stored.split(":")
        return hmac.compare_digest(_hash(password, bytes.fromhex(salt_hex)), h_hex)
    except Exception:  # noqa: BLE001
        return False


def refresh_users(db) -> None:
    """Přenačti aktivní účty do paměti (po každé změně v administraci)."""
    from sqlalchemy import select

    rows = db.execute(
        select(AppUser.id, AppUser.role, AppUser.token_version).where(AppUser.active.is_(True))
    ).all()
    settings.auth_users = {uid: (role, tv) for uid, role, tv in rows}


def set_password(password: str | None) -> None:
    """Nastav (nebo zruš při prázdném) heslo a otoč secret (zneplatní staré tokeny)."""
    db = SessionLocal()
    try:
        if not password:
            row = db.get(AppSetting, _PW_KEY)
            if row:
                db.delete(row)
            settings.auth_password_hash = None
        else:
            salt = secrets.token_bytes(16)
            stored = salt.hex() + ":" + _hash(password, salt)
            _put(db, _PW_KEY, stored)
            settings.auth_password_hash = stored
        # otoč secret → odhlásí staré relace
        sec = secrets.token_hex(32)
        _put(db, _SECRET_KEY, sec)
        settings.auth_secret = sec
        db.commit()
    finally:
        db.close()


def verify_password(password: str) -> bool:
    """Sdílené heslo (záložní cesta bez účtu)."""
    stored = settings.auth_password_hash
    if not stored:
        return True
    return check_password(password, stored)


def make_token(days: int = 30, *, user: AppUser | None = None) -> str:
    data: dict = {"exp": int(time.time()) + days * 86400}
    if user is not None:
        data.update({"uid": user.id, "name": user.username, "role": user.role,
                     "tv": user.token_version})
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    sig = hmac.new(settings.auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def token_info(token: str | None) -> dict | None:
    """Ověří podpis, expiraci a u účtu i to, že účet žije a heslo se nezměnilo.

    Vrací {"uid", "name", "role"}; token sdíleného hesla (bez uid) má roli
    admin – chová se jako dřív.
    """
    if not token:
        return None
    try:
        payload, sig = token.split(".")
        expect = hmac.new(
            settings.auth_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if float(data.get("exp", 0)) <= time.time():
            return None
    except Exception:  # noqa: BLE001
        return None
    uid = data.get("uid")
    if uid is None:
        return {"uid": None, "name": "", "role": "admin"}
    live = settings.auth_users.get(int(uid))
    if live is None or live[1] != data.get("tv"):
        return None
    return {"uid": int(uid), "name": data.get("name", ""), "role": live[0]}


def valid_token(token: str | None) -> bool:
    return token_info(token) is not None
