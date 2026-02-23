from __future__ import annotations

import os
from pathlib import Path

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

    try:
        _ = client.username
    except GarthException as exc:
        raise GarthException(f"Session expired for user {user_id}.") from exc

    return client


def save_client(client: garth.Client, user_id: int) -> None:
    tdir = token_dir(user_id)
    tdir.mkdir(parents=True, exist_ok=True)
    client.dump(str(tdir))
    os.chmod(tdir, 0o755)
    for f in tdir.iterdir():
        os.chmod(f, 0o644)


def login_and_save(user_id: int, email: str, password: str) -> garth.Client:
    client = garth.Client()
    client.login(email, password)
    save_client(client, user_id)
    return client


def is_authenticated(user_id: int) -> bool:
    try:
        get_client(user_id)
        return True
    except (GarthException, GarthHTTPError, Exception):
        return False
