"""
api/cache.py
────────────
SQLite-backed cache for team payloads + APScheduler background refresh.

The DB lives on the same Railway persistent volume as the garth tokens:
  $GARTH_SQUAD_HOME/squad_cache.db

Scheduled refresh times (CET = UTC+1, or CEST = UTC+2 in summer):
  07:00, 13:00, 17:00, 23:00 local time → stored as UTC offsets via
  cron triggers so they adjust automatically for DST.

Periods cached: thismonth, lastmonth, ytd
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("squad_stats.cache")

# Bump this whenever the payload schema changes — forces cache invalidation on deploy
CACHE_VERSION = "2"

# ── periods we cache ──────────────────────────────────────────────────────────
CACHED_PERIODS = ["thismonth", "lastmonth", "ytd"]

# ── CET refresh schedule (hour in CET/CEST local time) ───────────────────────
REFRESH_HOURS_CET = [7, 13, 17, 23]

# ── DB path ───────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    squad_home = Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))
    squad_home.mkdir(parents=True, exist_ok=True)
    return squad_home / "squad_cache.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS team_cache (
                period      TEXT NOT NULL,
                fetched_at  TEXT NOT NULL,
                payload     TEXT NOT NULL,
                version     TEXT NOT NULL DEFAULT '1',
                PRIMARY KEY (period)
            )
        """)
        # Migrate existing DB: add version column if absent
        try:
            conn.execute("ALTER TABLE team_cache ADD COLUMN version TEXT NOT NULL DEFAULT '1'")
            conn.commit()
        except Exception:
            pass  # Column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS refresh_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                period      TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                status      TEXT,
                error       TEXT
            )
        """)
        conn.commit()
    log.info("Cache DB initialised at %s", _db_path())


# ── read / write ──────────────────────────────────────────────────────────────

def get_cached(period: str) -> tuple[list[dict], str | None]:
    """
    Return (payload_list, fetched_at_iso) from cache, or ([], None) if empty or stale version.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at, version FROM team_cache WHERE period = ?",
                (period,)
            ).fetchone()
        if row and row["version"] == CACHE_VERSION:
            return json.loads(row["payload"]), row["fetched_at"]
    except Exception as exc:
        log.warning("Cache read failed for %s: %s", period, exc)
    return [], None


def set_cached(period: str, payload: list[dict]) -> None:
    """Write payload to cache, replacing any existing entry for that period."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO team_cache (period, fetched_at, payload, version)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(period) DO UPDATE SET
                     fetched_at = excluded.fetched_at,
                     payload    = excluded.payload,
                     version    = excluded.version""",
                (period, now, json.dumps(payload), CACHE_VERSION),
            )
            conn.commit()
        log.info("Cache updated for period=%s (%d users)", period, len(payload))
    except Exception as exc:
        log.error("Cache write failed for %s: %s", period, exc)


def cache_age_seconds(fetched_at: str | None) -> float | None:
    """Return how many seconds ago the cache was written, or None."""
    if not fetched_at:
        return None
    try:
        ts = datetime.fromisoformat(fetched_at)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


# ── background refresh ────────────────────────────────────────────────────────

_refresh_lock = threading.Lock()   # prevent overlapping refreshes


def refresh_all_periods(load_team_fn) -> None:
    """
    Fetch fresh data for all CACHED_PERIODS and write to DB.
    load_team_fn(period) → list[dict]  (provided by server.py to avoid circular import)
    """
    if not _refresh_lock.acquire(blocking=False):
        log.info("Refresh already running — skipping")
        return

    try:
        log.info("Background refresh started")
        for period in CACHED_PERIODS:
            started = datetime.now(timezone.utc).isoformat()
            try:
                data = load_team_fn(period)
                set_cached(period, data)
                status = "ok"
                error  = None
                log.info("Refreshed period=%s — %d users", period, len(data))
            except Exception as exc:
                status = "error"
                error  = str(exc)
                log.error("Refresh failed period=%s: %s", period, exc)
            finished = datetime.now(timezone.utc).isoformat()
            try:
                with _connect() as conn:
                    conn.execute(
                        """INSERT INTO refresh_log
                           (period, started_at, finished_at, status, error)
                           VALUES (?, ?, ?, ?, ?)""",
                        (period, started, finished, status, error),
                    )
                    conn.commit()
            except Exception:
                pass
        log.info("Background refresh complete")
    finally:
        _refresh_lock.release()


def start_scheduler(load_team_fn) -> None:
    """
    Start APScheduler with cron jobs at 07:00, 13:00, 17:00, 23:00 CET.
    CET = UTC+1.  We schedule in UTC so: 06:00, 12:00, 16:00, 22:00 UTC.
    (During CEST = UTC+2 these fire one hour early local time — acceptable trade-off
     without a full timezone library; add `timezone='Europe/Berlin'` if pytz is available.)
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("APScheduler not installed — background refresh disabled. "
                    "Add 'apscheduler>=3.10' to requirements.txt")
        return

    # UTC hours = CET hours - 1
    utc_hours = ",".join(str(h - 1) for h in REFRESH_HOURS_CET)

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=lambda: refresh_all_periods(load_team_fn),
        trigger=CronTrigger(hour=utc_hours, minute=0),
        id="team_refresh",
        name="Team data refresh",
        replace_existing=True,
        misfire_grace_time=300,   # allow up to 5 min late start
    )
    scheduler.start()
    log.info("Scheduler started — refresh at UTC hours: %s", utc_hours)


def last_refresh_log(limit: int = 10) -> list[dict]:
    """Return the last N refresh log entries (newest first)."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT period, started_at, finished_at, status, error
                   FROM refresh_log ORDER BY id DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
