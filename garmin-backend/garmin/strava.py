"""
garmin/strava.py
────────────────
Strava OAuth + activity fetching, mirroring the shape of the Garmin fetcher
so the same transform.build_user_payload() can be reused.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("squad_stats.strava")

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "205412")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_BASE          = "https://www.strava.com/api/v3"
REDIRECT_URI         = "https://fette-otter.up.railway.app/api/strava/callback"

# ── Strava activity type → our normalised type ───────────────────────────────
STRAVA_TYPE_MAP: dict[str, str] = {
    "run":                  "Running",
    "trail_run":            "Running",
    "treadmill":            "Running",
    "virtualrun":           "Running",
    "ride":                 "Cycling",
    "mountain_bike_ride":   "Cycling",
    "gravel_ride":          "Cycling",
    "handcycle":            "Cycling",
    "velomobile":           "Cycling",
    "virtualride":          "VirtualCycling",
    "ebikeride":            "VirtualCycling",
    "swim":                 "Swimming",
    "open_water_swimming":  "Swimming",
    "alpineski":            "Skiing",
    "backcountryski":       "Skiing",
    "nordicski":            "Skiing",
    "snowboard":            "Skiing",
    "snowshoe":             "Skiing",
    "walk":                 "Walking",
    "hike":                 "Walking",
}


# ── Token storage ─────────────────────────────────────────────────────────────

def _squad_home() -> Path:
    return Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))


def _token_path(user_id: int) -> Path:
    return _squad_home() / str(user_id) / "strava_token.json"


def save_token(user_id: int, token: dict[str, Any]) -> None:
    p = _token_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(token, indent=2))
    log.info("Strava token saved for user %s", user_id)


def load_token(user_id: int) -> dict[str, Any] | None:
    p = _token_path(user_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def is_authenticated(user_id: int) -> bool:
    return load_token(user_id) is not None


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def auth_url(state: str = "") -> str:
    """Build the Strava OAuth authorisation URL."""
    params = urllib.parse.urlencode({
        "client_id":     STRAVA_CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope":         "read,activity:read_all",
        "state":         state,
    })
    return f"https://www.strava.com/oauth/authorize?{params}"


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange authorisation code for tokens."""
    data = urllib.parse.urlencode({
        "client_id":     STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code":          code,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://www.strava.com/oauth/token",
        data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def refresh_token(user_id: int) -> dict[str, Any]:
    """Refresh expired access token and save."""
    token = load_token(user_id)
    if not token:
        raise ValueError(f"No Strava token for user {user_id}")
    data = urllib.parse.urlencode({
        "client_id":     STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": token["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        "https://www.strava.com/oauth/token",
        data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        new_token = json.loads(resp.read())
    save_token(user_id, new_token)
    return new_token


def get_access_token(user_id: int) -> str:
    """Return a valid access token, refreshing if expired."""
    token = load_token(user_id)
    if not token:
        raise ValueError(f"No Strava token for user {user_id}")
    # Refresh if within 5 minutes of expiry
    expires_at = token.get("expires_at", 0)
    if expires_at - 300 < datetime.now(timezone.utc).timestamp():
        token = refresh_token(user_id)
    return token["access_token"]


# ── API calls ─────────────────────────────────────────────────────────────────

def _get(path: str, access_token: str, params: dict | None = None) -> Any:
    url = STRAVA_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_athlete(user_id: int) -> dict[str, Any]:
    return _get("/athlete", get_access_token(user_id))


def fetch_activities(user_id: int, after: date, before: date | None = None) -> list[dict[str, Any]]:
    """Fetch all activities between after and before (inclusive)."""
    access_token = get_access_token(user_id)
    after_ts  = int(datetime(after.year, after.month, after.day, tzinfo=timezone.utc).timestamp())
    before_ts = int(datetime(
        (before or date.today()).year,
        (before or date.today()).month,
        (before or date.today()).day,
        23, 59, 59, tzinfo=timezone.utc,
    ).timestamp())

    activities = []
    page = 1
    while True:
        batch = _get("/athlete/activities", access_token, {
            "after":    after_ts,
            "before":   before_ts,
            "per_page": 100,
            "page":     page,
        })
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return activities


# ── Normalise to the same shape as garmin._normalise_activity ────────────────

def normalise_activity(act: dict[str, Any]) -> dict[str, Any]:
    raw_type = (act.get("sport_type") or act.get("type") or "").lower()
    mapped   = STRAVA_TYPE_MAP.get(raw_type, raw_type.replace("_", " ").title())
    start    = (act.get("start_date_local") or "")[:10]
    return {
        "id":          act.get("id"),
        "name":        act.get("name", ""),
        "type_raw":    raw_type,
        "type":        mapped,
        "date":        start,
        "calories":    int(act.get("calories") or 0),
        "active_kcal": int(act.get("calories") or 0),   # Strava only has total calories
        "distance_m":  float(act.get("distance") or 0),
        "duration_s":  float(act.get("moving_time") or 0),
    }


def fetch_and_normalise(user_id: int, after: date, before: date | None = None) -> list[dict[str, Any]]:
    raw = fetch_activities(user_id, after, before)
    return [normalise_activity(a) for a in raw]
