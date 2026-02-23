from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PALETTE = [
    {"color": "#7c3aed", "bg": "#ede9fe", "emoji": "🦁"},
    {"color": "#db2777", "bg": "#fce7f3", "emoji": "🐯"},
    {"color": "#0284c7", "bg": "#e0f2fe", "emoji": "🦊"},
    {"color": "#b45309", "bg": "#fef3c7", "emoji": "🐺"},
    {"color": "#059669", "bg": "#d1fae5", "emoji": "🦅"},
    {"color": "#0e7490", "bg": "#cffafe", "emoji": "🐬"},
    {"color": "#be185d", "bg": "#fdf2f8", "emoji": "🦋"},
    {"color": "#d97706", "bg": "#fffbeb", "emoji": "🐉"},
    {"color": "#4f46e5", "bg": "#eef2ff", "emoji": "🦄"},
    {"color": "#0891b2", "bg": "#ecfeff", "emoji": "🐋"},
    {"color": "#16a34a", "bg": "#f0fdf4", "emoji": "🦎"},
    {"color": "#dc2626", "bg": "#fef2f2", "emoji": "🐆"},
]

_lock = threading.RLock()


def _squad_home() -> Path:
    """Always read fresh from env — critical for containers."""
    return Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))


def _registry_file() -> Path:
    return _squad_home() / "members.json"


def _load() -> list[dict[str, Any]]:
    _squad_home().mkdir(parents=True, exist_ok=True)
    rf = _registry_file()
    if not rf.exists():
        return []
    try:
        with open(rf) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(members: list[dict[str, Any]]) -> None:
    home = _squad_home()
    home.mkdir(parents=True, exist_ok=True)
    rf   = _registry_file()
    tmp  = rf.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(members, f, indent=2)
    os.replace(str(tmp), str(rf))  # atomic rename
    try:
        os.chmod(rf, 0o644)
    except OSError:
        pass


def all_members() -> list[dict[str, Any]]:
    with _lock:
        return _load()


def get_member(member_id: int) -> dict[str, Any] | None:
    with _lock:
        return next((m for m in _load() if m["id"] == member_id), None)


def get_by_google_sub(sub: str) -> dict[str, Any] | None:
    with _lock:
        return next((m for m in _load() if m.get("google_sub") == sub), None)


def add_member(
    google_sub: str,
    google_email: str,
    name: str,
    picture: str,
    garmin_email: str,
    role: str = "",
) -> dict[str, Any]:
    with _lock:
        members = _load()
        existing = next((m for m in members if m["google_sub"] == google_sub), None)
        if existing:
            raise ValueError(f"Member already exists (id={existing['id']})")

        new_id  = (max((m["id"] for m in members), default=0)) + 1
        palette = PALETTE[(new_id - 1) % len(PALETTE)]

        member: dict[str, Any] = {
            "id":           new_id,
            "google_sub":   google_sub,
            "google_email": google_email,
            "garmin_email": garmin_email,
            "name":         name,
            "picture":      picture,
            "role":         role or "Brew Crew",
            "emoji":        palette["emoji"],
            "color":        palette["color"],
            "bg":           palette["bg"],
            "garminDevice": "Garmin",
            "types":        [],
            "joined_at":    datetime.now(timezone.utc).isoformat(),
        }

        members.append(member)
        _save(members)
        return member


def update_member(member_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        members = _load()
        for i, m in enumerate(members):
            if m["id"] == member_id:
                members[i] = {**m, **updates}
                _save(members)
                return members[i]
        return None


def remove_member(member_id: int) -> bool:
    with _lock:
        members = _load()
        new = [m for m in members if m["id"] != member_id]
        if len(new) == len(members):
            return False
        _save(new)
        return True
