#!/usr/bin/env python3
"""
token_export.py
───────────────
Exports local Garmin tokens as a single base64-encoded environment variable
so they can be injected into your cloud container without storing passwords.

Usage
─────
  # 1. Authenticate locally first (if not already done)
  python auth_setup.py --user 1

  # 2. Export tokens to an env var
  python token_export.py --export
  # → Prints: GARTH_TOKENS_B64=eyJtZW1iZXJz...

  # 3. Set that variable in Railway / Render / Fly dashboard
  #    The server auto-imports it on startup.

  # 4. To verify what's in the env var:
  python token_export.py --inspect

How it works
────────────
  On startup, api/server.py calls import_tokens_from_env() which reads
  GARTH_TOKENS_B64, decodes it, and writes the token files to GARTH_SQUAD_HOME.
  This means no secrets are baked into the image — they live as env vars.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

GARTH_SQUAD_HOME = Path(
    os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad")
)


def export_tokens() -> str:
    """
    Read all token files from GARTH_SQUAD_HOME and encode as base64 JSON.
    Returns the value to set as GARTH_TOKENS_B64.
    """
    if not GARTH_SQUAD_HOME.exists():
        print(f"❌ Token directory not found: {GARTH_SQUAD_HOME}")
        print("   Run `python auth_setup.py --all` first to authenticate.")
        sys.exit(1)

    bundle: dict[str, dict[str, str]] = {}

    # Walk user subdirectories
    for user_dir in sorted(GARTH_SQUAD_HOME.iterdir()):
        if not user_dir.is_dir():
            continue
        uid = user_dir.name
        files: dict[str, str] = {}
        for token_file in user_dir.iterdir():
            if token_file.is_file():
                files[token_file.name] = token_file.read_text()
        if files:
            bundle[uid] = files

    # Also include members.json
    members_file = GARTH_SQUAD_HOME / "members.json"
    if members_file.exists():
        bundle["__members__"] = {"members.json": members_file.read_text()}

    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()
    return encoded


def inspect_tokens(encoded: str) -> None:
    """Pretty-print the contents of a GARTH_TOKENS_B64 value."""
    try:
        bundle = json.loads(base64.b64decode(encoded))
    except Exception as e:
        print(f"❌ Could not decode: {e}")
        sys.exit(1)

    for uid, files in bundle.items():
        print(f"\n  📁  {uid}/")
        for fname in files:
            print(f"       • {fname}")


def import_tokens_from_env(tokens_b64: str, dest: Path) -> None:
    """
    Called at server startup: decode GARTH_TOKENS_B64 and write token files.
    Safe to call on every restart — only writes if file doesn't exist or differs.
    """
    try:
        bundle = json.loads(base64.b64decode(tokens_b64))
    except Exception as e:
        print(f"⚠️  GARTH_TOKENS_B64 decode failed: {e}", flush=True)
        return

    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    written = 0

    for uid, files in bundle.items():
        if uid == "__members__":
            for fname, content in files.items():
                fp = dest / fname
                if not fp.exists() or fp.read_text() != content:
                    fp.write_text(content)
                    os.chmod(fp, 0o600)
                    written += 1
        else:
            user_dir = dest / uid
            user_dir.mkdir(exist_ok=True)
            os.chmod(user_dir, 0o700)
            for fname, content in files.items():
                fp = user_dir / fname
                if not fp.exists() or fp.read_text() != content:
                    fp.write_text(content)
                    os.chmod(fp, 0o600)
                    written += 1

    if written:
        print(f"✅ Imported {written} token file(s) from GARTH_TOKENS_B64", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Garmin token importer/exporter")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export",  action="store_true", help="Export tokens to env var")
    group.add_argument("--inspect", metavar="B64",       help="Inspect a GARTH_TOKENS_B64 value")
    args = parser.parse_args()

    if args.export:
        encoded = export_tokens()
        print(f"\n✅ Copy this into your cloud provider's environment variables:\n")
        print(f"GARTH_TOKENS_B64={encoded}\n")
        print(f"({len(encoded)} chars, covers {encoded.count('{')} user(s))")

    elif args.inspect:
        inspect_tokens(args.inspect)
