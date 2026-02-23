from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.parse
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from pathlib import Path

# ── Path setup — must happen before any local imports ────────────────────────
# /app/
#   wsgi.py
#   api/server.py   ← this file
#   garmin/
#   token_export.py
_HERE = Path(__file__).resolve().parent        # /app/api
_ROOT = _HERE.parent                            # /app
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from garth.exc import GarthException, GarthHTTPError
import garmin as g

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("squad_stats")

# ── Token bootstrap (cloud deployments) ──────────────────────────────────────
_tokens_b64 = os.environ.get("GARTH_TOKENS_B64", "")
if _tokens_b64:
    try:
        from token_export import import_tokens_from_env
        _squad_home = Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))
        import_tokens_from_env(_tokens_b64, _squad_home)
    except Exception as _e:
        print(f"⚠️  Token bootstrap failed: {_e}", flush=True)

app = Flask(__name__)
CORS(app, origins="*")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
RANGE_DAYS = {"today": 1, "1w": 7, "4w": 28}


def _range_days(p: str) -> int:
    return RANGE_DAYS.get(p, 7)


def verify_google_id_token(id_token: str) -> dict[str, Any]:
    url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(id_token)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        raise ValueError(f"Token request failed: {exc}") from exc
    if payload.get("error_description"):
        raise ValueError(f"Invalid token: {payload['error_description']}")
    if GOOGLE_CLIENT_ID and payload.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("Token audience mismatch")
    return payload


def load_user_data(member: dict[str, Any], range_days: int) -> dict[str, Any] | None:
    uid = member["id"]
    try:
        client = g.get_client(uid)
    except GarthException as exc:
        log.warning("User %s unauthenticated: %s", uid, exc)
        return None

    today = date.today()
    start = today - timedelta(days=range_days - 1)

    try:
        week_acts      = g.fetch_activities_last_n_days(client, range_days)
        week_summaries = g.fetch_daily_summaries(client, start, range_days)
        bmi            = g.fetch_latest_bmi(client)

        months_to_fetch = []
        for i in range(11, -1, -1):
            m_date = date(today.year, today.month, 1) - timedelta(days=30 * i)
            months_to_fetch.append((m_date.year, m_date.month))

        monthly_acts: dict[str, list] = {}
        monthly_bmis: dict[str, Any] = {}

        def _fetch_month(yr, mo):
            key = f"{yr}-{mo:02d}"
            acts = g.fetch_activities_for_month(client, yr, mo)
            return key, acts, bmi

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_month, yr, mo): (yr, mo)
                       for yr, mo in months_to_fetch}
            for fut in as_completed(futures):
                try:
                    key, acts, m_bmi = fut.result()
                    monthly_acts[key] = acts
                    monthly_bmis[key] = m_bmi
                except Exception as exc:
                    yr, mo = futures[fut]
                    monthly_acts[f"{yr}-{mo:02d}"] = []
                    monthly_bmis[f"{yr}-{mo:02d}"] = bmi

        return g.build_user_payload(
            roster_entry=member,
            week_activities=week_acts,
            week_summaries=week_summaries,
            monthly_activities=monthly_acts,
            monthly_bmis=monthly_bmis,
            range_days=range_days,
            bmi=bmi,
        )
    except Exception as exc:
        log.exception("Garmin fetch failed user %s: %s", uid, exc)
        return None


def _stub(member: dict[str, Any]) -> dict[str, Any]:
    z = {"cal": 0, "sess": 0, "km": 0.0, "actKcal": 0, "bmi": None, "days": [0]*28}
    safe = ("id","name","role","emoji","color","bg","garminDevice","types","picture","google_email")
    return {
        **{k: member.get(k,"") for k in safe},
        "calories":0,"workouts":0,"km":0.0,"actKcal":0,"bmi":0.0,
        "week":[0]*7,"weekCalories":[0]*7,
        "monthly":[dict(z) for _ in range(12)],
        "_stub": True,
    }


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "team_size": len(g.all_members())})


@app.get("/api/status")
def auth_status():
    return jsonify([
        {"id": m["id"], "name": m["name"], "authenticated": g.is_authenticated(m["id"])}
        for m in g.all_members()
    ])


@app.get("/api/members")
def list_members():
    safe = ("id","name","role","emoji","color","bg","garminDevice",
            "types","picture","google_email","joined_at")
    return jsonify([{k: m.get(k) for k in safe} for m in g.all_members()])


@app.post("/api/members/join")
def join():
    body            = request.get_json(silent=True) or {}
    name            = (body.get("name")            or "").strip()
    garmin_email    = (body.get("garmin_email")    or "").strip()
    garmin_password = (body.get("garmin_password") or "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not garmin_email or not garmin_password:
        return jsonify({"error": "garmin_email and garmin_password are required"}), 400

    # Check if Garmin email already registered
    existing = next((m for m in g.all_members() if m.get("garmin_email") == garmin_email), None)
    if existing:
        # If already authenticated, just return their profile
        if g.is_authenticated(existing["id"]):
            safe = {k: existing.get(k) for k in
                    ("id","name","role","emoji","color","bg","garminDevice",
                     "types","picture","google_email","joined_at")}
            return jsonify({"member": safe, "message": "Already in the squad!", "rejoined": True}), 200
        # Member exists but token is missing — re-authenticate
        try:
            g.login_and_save(existing["id"], garmin_email, garmin_password)
            safe = {k: existing.get(k) for k in
                    ("id","name","role","emoji","color","bg","garminDevice",
                     "types","picture","google_email","joined_at")}
            return jsonify({"member": safe, "message": "Welcome back! 🎉", "rejoined": True}), 200
        except Exception as exc:
            return jsonify({"error": f"Garmin Connect login failed — check your email and password. ({exc})"}), 422

    # Create member record
    try:
        new_member = g.add_member(
            google_sub=f"garmin_{garmin_email}",
            google_email=garmin_email,
            name=name,
            picture="",
            garmin_email=garmin_email,
            role="Brew Crew",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    # Authenticate Garmin
    try:
        g.login_and_save(new_member["id"], garmin_email, garmin_password)
    except Exception as exc:
        g.remove_member(new_member["id"])
        return jsonify({"error": f"Garmin Connect login failed — check your email and password. ({exc})"}), 422

    safe = {k: new_member.get(k) for k in
            ("id","name","role","emoji","color","bg","garminDevice",
             "types","picture","google_email","joined_at")}
    return jsonify({"member": safe, "message": "Welcome to Brew Crew! 🎉"}), 201


@app.delete("/api/members/<int:member_id>")
def remove_member_route(member_id: int):
    body     = request.get_json(silent=True) or {}
    id_token = (body.get("id_token") or "").strip()
    if not id_token:
        return jsonify({"error": "id_token required"}), 400
    try:
        payload = verify_google_id_token(id_token)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401

    member = g.get_member(member_id)
    if not member:
        return jsonify({"error": "Member not found"}), 404
    if member.get("google_sub") != payload.get("sub"):
        return jsonify({"error": "Forbidden"}), 403

    g.remove_member(member_id)
    return jsonify({"message": f"Removed {member['name']} from the squad"}), 200


@app.get("/api/team")
def get_team():
    members = g.all_members()
    if not members:
        return jsonify([])
    rd = _range_days(request.args.get("range", "1w"))
    results = []
    with ThreadPoolExecutor(max_workers=max(len(members), 1)) as pool:
        fmap = {pool.submit(load_user_data, m, rd): m for m in members}
        for fut in as_completed(fmap):
            member = fmap[fut]
            try:
                payload = fut.result()
            except Exception:
                payload = None
            if payload is None:
                payload = _stub(member)
            payload["picture"]      = member.get("picture", "")
            payload["google_email"] = member.get("google_email", "")
            results.append(payload)
    id_order = {m["id"]: i for i, m in enumerate(members)}
    results.sort(key=lambda u: id_order.get(u["id"], 999))
    return jsonify(results)


@app.get("/api/user/<int:user_id>")
def get_user(user_id: int):
    member = g.get_member(user_id)
    if not member:
        abort(404)
    rd      = _range_days(request.args.get("range", "1w"))
    payload = load_user_data(member, rd) or _stub(member)
    payload["picture"]      = member.get("picture", "")
    payload["google_email"] = member.get("google_email", "")
    return jsonify(payload)


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Brew Crew API — port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
