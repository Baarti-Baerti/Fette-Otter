"""
garmin/session.py
─────────────────
Per-user garth session management.

Each team member authenticates independently and their OAuth tokens are
stored under ~/.garth_squad/<user_id>/  so the server can resume sessions
without re-authenticating on every request.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import garth
from garth.exc import GarthException, GarthHTTPError

# Root directory for storing per-user garth token directories
GARTH_SQUAD_HOME = Path(
    os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad")
)


def token_dir(user_id: int) -> Path:
    """Return the token storage directory for a given user ID."""
    return GARTH_SQUAD_HOME / str(user_id)


def get_client(user_id: int) -> garth.Client:
    """
    Return an authenticated garth Client for the given user.

    Tries to resume from saved tokens. Raises GarthException if no tokens
    exist or if the session has expired (caller should re-authenticate).
    """
    tdir = token_dir(user_id)
    if not tdir.exists():
        raise GarthException(
            f"No tokens found for user {user_id}. "
            f"Run `python auth_setup.py --user {user_id}` to authenticate."
        )

    client = garth.Client()
    client.load(str(tdir))

    # Validate session is still alive (lightweight call)
    try:
        _ = client.username
    except GarthException as exc:
        raise GarthException(
            f"Session expired for user {user_id}. "
            f"Re-run `python auth_setup.py --user {user_id}`."
        ) from exc

    return client


def save_client(client: garth.Client, user_id: int) -> None:
    """Persist an authenticated client's tokens to disk."""
    tdir = token_dir(user_id)
    tdir.mkdir(parents=True, exist_ok=True)
    client.dump(str(tdir))
    # Lock down permissions
    os.chmod(tdir, 0o700)
    for f in tdir.iterdir():
        os.chmod(f, 0o600)


def login_and_save(user_id: int, email: str, password: str) -> garth.Client:
    """
    Perform a full login for a user and save the resulting tokens.
    Returns the authenticated Client.
    """
    client = garth.Client()
    client.login(email, password)
    save_client(client, user_id)
    return client


def is_authenticated(user_id: int) -> bool:
    """Return True if valid tokens exist for this user."""
    try:
        get_client(user_id)
        return True
    except (GarthException, GarthHTTPError):
        return False
