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
from .models import AppSetting

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
    sec = _get(db, _SECRET_KEY)
    if not sec:
        sec = secrets.token_hex(32)
        _put(db, _SECRET_KEY, sec)
        db.commit()
    settings.auth_secret = sec


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER).hex()


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
    stored = settings.auth_password_hash
    if not stored:
        return True
    try:
        salt_hex, h_hex = stored.split(":")
        return hmac.compare_digest(_hash(password, bytes.fromhex(salt_hex)), h_hex)
    except Exception:  # noqa: BLE001
        return False


def make_token(days: int = 30) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + days * 86400}).encode()
    ).decode()
    sig = hmac.new(settings.auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        payload, sig = token.split(".")
        expect = hmac.new(
            settings.auth_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp", 0)) > time.time()
    except Exception:  # noqa: BLE001
        return False
