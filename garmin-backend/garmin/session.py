from __future__ import annotations

import json
import os
import pickle
import secrets
import time
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


# ── Disk-based pending MFA sessions ──────────────────────────────────────────
# Stored as pickle files in GARTH_SQUAD_HOME/.mfa/<token>.pkl
# so they survive across gunicorn worker processes.
# TTL: 10 minutes — plenty of time for the user to find the OTP code.

_MFA_TTL = 600  # seconds


def _mfa_dir() -> Path:
    d = _squad_home() / ".mfa"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_pending_mfa(client: garth.Client, resume_data: Any,
                      user_id: int, member: dict, is_new: bool) -> str:
    """Pickle the partial MFA session to disk; returns a token for the client."""
    token = secrets.token_urlsafe(32)
    payload = {
        "client":      client,
        "resume_data": resume_data,
        "user_id":     user_id,
        "member":      member,
        "is_new":      is_new,
        "expires_at":  time.time() + _MFA_TTL,
    }
    path = _mfa_dir() / f"{token}.pkl"
    with open(path, "wb") as f:
        pickle.dump(payload, f)
    os.chmod(path, 0o600)
    return token


def complete_mfa_login(mfa_token: str, otp_code: str):
    """
    Load the pending session from disk, complete login with OTP.
    Raises KeyError if token unknown/expired, GarthHTTPError if OTP wrong.
    """
    path = _mfa_dir() / f"{mfa_token}.pkl"
    if not path.exists():
        raise KeyError("MFA session not found — please start login again.")

    with open(path, "rb") as f:
        pending = pickle.load(f)

    # Delete immediately so it can't be reused
    path.unlink(missing_ok=True)

    if time.time() > pending["expires_at"]:
        raise KeyError("MFA session expired — please start login again.")

    client: garth.Client = pending["client"]
    resume_data = pending["resume_data"]
    client.resume_login(resume_data, otp_code)
    return client, pending["user_id"], pending["member"], pending["is_new"]


def login_start(email: str, password: str) -> tuple[str, Any]:
    """
    Begin login. Returns ("ok", client) on success,
    or ("needs_mfa", (client, resume_data)) when 2FA is required.
    """
    client = garth.Client()
    result = client.login(email, password, return_on_mfa=True)

    if isinstance(result, tuple) and len(result) == 2 and result[0] == "needs_mfa":
        return "needs_mfa", (client, result[1])

    return "ok", client


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
