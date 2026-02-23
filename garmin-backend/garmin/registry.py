"""
garmin/registry.py
──────────────────
Persistent member registry backed by a JSON file.

Replaces the static config/team.py so members can be added/removed at runtime
via the API without restarting the server.

Storage: ~/.garth_squad/members.json
Format:
[
  {
    "id": 1,
    "google_sub": "1234567890",       ← Google account subject ID
    "google_email": "alex@gmail.com",
    "name": "Alex Chen",
    "picture": "https://…",           ← Google profile photo URL
    "role": "Engineering",
    "garmin_email": "alex@garmin.com",
    "emoji": "🦁",
    "color": "#7c3aed",
    "bg": "#ede9fe",
    "garminDevice": "Forerunner 965",
    "types": [],
    "joined_at": "2026-02-23T14:00:00"
  },
  …
]
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GARTH_SQUAD_HOME = Path(
    os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad")
)
REGISTRY_FILE = GARTH_SQUAD_HOME / "members.json"

# Palette assigned round-robin to new members
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
    {"color": "#dc2626", "bg": "#fef2f2", "emoji": "🦁"},
]

_lock = threading.RLock()


def _load() -> list[dict[str, Any]]:
    GARTH_SQUAD_HOME.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_FILE.exists():
        return []
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(members: list[dict[str, Any]]) -> None:
    GARTH_SQUAD_HOME.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(members, f, indent=2)
    tmp.replace(REGISTRY_FILE)
    os.chmod(REGISTRY_FILE, 0o600)


def all_members() -> list[dict[str, Any]]:
    """Return all registered members."""
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
    """
    Register a new member. Raises ValueError if google_sub already exists.
    Returns the newly created member dict.
    """
    with _lock:
        members = _load()

        # Idempotency check
        existing = next((m for m in members if m["google_sub"] == google_sub), None)
        if existing:
            raise ValueError(f"Member with Google account already exists (id={existing['id']})")

        new_id = (max((m["id"] for m in members), default=0)) + 1
        palette = PALETTE[(new_id - 1) % len(PALETTE)]

        member: dict[str, Any] = {
            "id":           new_id,
            "google_sub":   google_sub,
            "google_email": google_email,
            "garmin_email": garmin_email,
            "name":         name,
            "picture":      picture,
            "role":         role or google_email.split("@")[0].replace(".", " ").title(),
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
    """Update fields on an existing member. Returns updated member or None."""
    with _lock:
        members = _load()
        for i, m in enumerate(members):
            if m["id"] == member_id:
                members[i] = {**m, **updates}
                _save(members)
                return members[i]
        return None


def remove_member(member_id: int) -> bool:
    """Remove a member by id. Returns True if removed."""
    with _lock:
        members = _load()
        new = [m for m in members if m["id"] != member_id]
        if len(new) == len(members):
            return False
        _save(new)
        return True
