from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import Settings, get_settings


SESSION_COOKIE = "focus_session"
CSRF_COOKIE = "focus_csrf"
SESSION_TTL_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class Identity:
    email: str
    role: str
    google_sub: str


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session(identity: Identity, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    payload = {
        "email": identity.email.lower().strip(),
        "role": identity.role,
        "sub": identity.google_sub,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(settings.app_secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_session(token: str | None, settings: Settings | None = None) -> Identity | None:
    if not token:
        return None
    settings = settings or get_settings()
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(settings.app_secret.encode(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            return None
        payload = json.loads(_b64decode(encoded))
        if int(payload["exp"]) < int(time.time()):
            return None
        return Identity(email=payload["email"], role=payload["role"], google_sub=payload["sub"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def verify_google_credential(credential: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    if not settings.google_oauth_client_id:
        raise RuntimeError("Falta GOOGLE_OAUTH_CLIENT_ID")
    info = id_token.verify_oauth2_token(
        credential,
        google_requests.Request(),
        settings.google_oauth_client_id,
    )
    if not info.get("email_verified"):
        raise ValueError("Google no ha verificado este correo")
    return info


def require_identity(request: Request) -> Identity:
    identity = verify_session(request.cookies.get(SESSION_COOKIE))
    if not identity:
        raise HTTPException(status_code=401, detail="Inicia sesion para continuar")
    return identity


def validate_csrf(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="Solicitud no valida")

