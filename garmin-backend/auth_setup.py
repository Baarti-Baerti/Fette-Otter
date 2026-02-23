#!/usr/bin/env python3
"""
auth_setup.py
─────────────
One-time authentication setup for each team member.

Usage:
    # Authenticate a specific user (prompts for password)
    python auth_setup.py --user 1

    # Authenticate all users in the roster
    python auth_setup.py --all

    # Check auth status of all users
    python auth_setup.py --status

Each user's OAuth tokens are saved to ~/.garth_squad/<id>/
and will auto-refresh for ~1 year before needing to re-run.
"""

import argparse
import getpass
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from config.team import TEAM
from garmin.session import login_and_save, is_authenticated, token_dir


def authenticate_user(member: dict) -> bool:
    uid   = member["id"]
    name  = member["name"]
    email = member["email"]

    print(f"\n{'─'*50}")
    print(f"  Authenticating: {member['emoji']} {name}")
    print(f"  Email: {email}")
    print(f"  Token dir: {token_dir(uid)}")
    print(f"{'─'*50}")

    # Check if already authenticated
    if is_authenticated(uid):
        answer = input(f"  ✅ {name} is already authenticated. Re-authenticate? [y/N] ").strip().lower()
        if answer != "y":
            print(f"  Skipped.")
            return True

    password = getpass.getpass(f"  Garmin Connect password for {email}: ")

    try:
        client = login_and_save(uid, email, password)
        print(f"  ✅ Authentication successful! Tokens saved to {token_dir(uid)}")
        print(f"  👤 Logged in as: {client.username}")
        return True
    except Exception as exc:
        print(f"  ❌ Authentication failed: {exc}")
        return False


def print_status():
    print("\nSquad Stats — Authentication Status")
    print("═" * 40)
    all_ok = True
    for member in TEAM:
        ok = is_authenticated(member["id"])
        icon = "✅" if ok else "❌"
        status = "authenticated" if ok else "NOT authenticated"
        print(f"  {icon}  {member['emoji']} {member['name']:20s}  {status}")
        if not ok:
            all_ok = False
    print("═" * 40)
    if not all_ok:
        print("\n  Run `python auth_setup.py --user <id>` to authenticate missing users.\n")
    else:
        print("\n  All users authenticated! Run `python api/server.py` to start the API.\n")


def main():
    parser = argparse.ArgumentParser(
        description="Authenticate Garmin Connect accounts for Squad Stats"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--user",   type=int,  metavar="ID",  help="Authenticate a specific user by ID")
    group.add_argument("--all",    action="store_true",       help="Authenticate all team members")
    group.add_argument("--status", action="store_true",       help="Show authentication status")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.user:
        member = next((m for m in TEAM if m["id"] == args.user), None)
        if not member:
            print(f"❌ User ID {args.user} not found in roster.")
            print(f"   Valid IDs: {[m['id'] for m in TEAM]}")
            sys.exit(1)
        success = authenticate_user(member)
        sys.exit(0 if success else 1)

    if args.all:
        print("\nAuthenticating all team members...")
        results = []
        for member in TEAM:
            ok = authenticate_user(member)
            results.append((member["name"], ok))

        print("\n\nSummary:")
        print("─" * 40)
        for name, ok in results:
            icon = "✅" if ok else "❌"
            print(f"  {icon}  {name}")
        failed = [n for n, ok in results if not ok]
        if failed:
            print(f"\n  {len(failed)} user(s) failed. Re-run with --user <id> to retry.")
            sys.exit(1)
        else:
            print("\n  All authenticated! ✨\n")


if __name__ == "__main__":
    main()
