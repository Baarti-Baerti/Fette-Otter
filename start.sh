#!/bin/sh
# Fix volume permissions at runtime (Railway mounts volumes as root)
mkdir -p /data/garth_squad
chown -R appuser:appuser /data
chmod 700 /data/garth_squad

# Drop to appuser and start the app
exec gosu appuser python /app/api/server.py
