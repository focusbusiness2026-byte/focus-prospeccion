"""Short-lived handoff from the owner-private sandbox portal; no auth bypass."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time


def verify_handoff(token,secret,audience,clock=time.time):
    if len(secret)<32:raise ValueError("Configuración de autenticación incompleta")
    try:
        raw,signature=token.split(".")
        expected=hmac.new(secret.encode(),raw.encode(),hashlib.sha256).digest()
        actual=base64.urlsafe_b64decode(signature+"="*(-len(signature)%4))
        if not hmac.compare_digest(expected,actual):raise ValueError()
        claims=json.loads(base64.urlsafe_b64decode(raw+"="*(-len(raw)%4)))
        now=clock()
        if claims.get("aud")!=audience or claims.get("tenant")!="three-dots" or claims.get("sub")!="sandbox-owner":raise ValueError()
        if not isinstance(claims.get("exp"),(int,float)) or not 0<claims["exp"]-now<=90:raise ValueError()
        if not isinstance(claims.get("nonce"),str) or not 24<=len(claims["nonce"])<=100:raise ValueError()
        return claims
    except (ValueError,TypeError,KeyError,json.JSONDecodeError):
        raise ValueError("Enlace de sesión inválido o caducado") from None
