from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import garth
from garth.exc import GarthException, GarthHTTPError


def _squad_home() -> Path:
    """Always read GARTH_SQUAD_HOME fresh from env so it works in containers."""
    return Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))


def token_dir(user_id: int) -> Path:
    return _squad_home() / str(user_id)


def get_client(user_id: int) -> garth.Client:
    tdir = token_dir(user_id)
    if not tdir.exists():
        raise GarthException(f"No tokens found for user {user_id}.")

    client = garth.Client()
    client.load(str(tdir))
    return client


def save_client(client: garth.Client, user_id: int) -> None:
    tdir = token_dir(user_id)
    tdir.mkdir(parents=True, exist_ok=True)
    client.dump(str(tdir))
    os.chmod(tdir, 0o755)
    for f in tdir.iterdir():
        os.chmod(f, 0o644)


# ── In-memory store for pending MFA sessions ─────────────────────
# { mfa_token: { "client": garth.Client, "resume_data": any,
#                "user_id": int, "member": dict, "is_new": bool } }
_pending_mfa: dict[str, dict[str, Any]] = {}


def login_start(email: str, password: str) -> tuple[str, Any]:
    """
    Begin login. Returns ("ok", client) on success,
    or ("needs_mfa", (client, resume_data)) when 2FA is required.
    """
    client = garth.Client()
    result = client.login(email, password, return_on_mfa=True)

    if isinstance(result, tuple) and len(result) == 2 and result[0] == "needs_mfa":
        return "needs_mfa", (client, result[1])

    # No MFA — login completed
    return "ok", client


def store_pending_mfa(client: garth.Client, resume_data: Any,
                      user_id: int, member: dict, is_new: bool) -> str:
    """Store a partial MFA session; returns a short-lived token for the client."""
    token = secrets.token_urlsafe(32)
    _pending_mfa[token] = {
        "client":      client,
        "resume_data": resume_data,
        "user_id":     user_id,
        "member":      member,
        "is_new":      is_new,
    }
    return token


def complete_mfa_login(mfa_token: str, otp_code: str) -> garth.Client:
    """
    Complete a pending MFA login with the OTP code.
    Raises KeyError if token unknown, GarthHTTPError if OTP wrong.
    """
    pending = _pending_mfa.pop(mfa_token, None)
    if pending is None:
        raise KeyError("MFA session not found or expired — please start login again.")

    client: garth.Client = pending["client"]
    resume_data = pending["resume_data"]

    client.resume_login(resume_data, otp_code)
    return client, pending["user_id"], pending["member"], pending["is_new"]


def login_and_save(user_id: int, email: str, password: str) -> garth.Client:
    status, result = login_start(email, password)
    if status == "needs_mfa":
        raise ValueError("MFA_REQUIRED")
    save_client(result, user_id)
    return result


def is_authenticated(user_id: int) -> bool:
    try:
        get_client(user_id)
        return True
    except (GarthException, GarthHTTPError, Exception):
        return False
