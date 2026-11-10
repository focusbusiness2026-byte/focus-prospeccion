"""Hosting authorization never implies a live email send or credential rotation."""
from types import SimpleNamespace

import pytest

from app.outreach_setup import outreach_ai_authorized


@pytest.mark.parametrize("real", [False, True])
@pytest.mark.parametrize("enabled", [None, "0", "1"])
@pytest.mark.parametrize("rotation,reuse", [(None, None), ("0", "0"), ("1", None), (None, "1"), ("true", "true")])
def test_paid_drafts_require_real_mode_explicit_ai_and_credential_authorization(monkeypatch, real, enabled, rotation, reuse):
    for key, value in {
        "OUTREACH_AI_ENABLED": enabled,
        "OUTREACH_KEY_ROTATION_CONFIRMED": rotation,
        "OUTREACH_EXISTING_KEY_REUSE_AUTHORIZED": reuse,
    }.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert outreach_ai_authorized(SimpleNamespace(real=real)) == (
        real and enabled == "1" and (rotation == "1" or reuse == "1")
    )
    assert __import__("os").getenv("OUTREACH_KEY_ROTATION_CONFIRMED") == rotation
