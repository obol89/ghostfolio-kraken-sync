#!/bin/bash
set -e

if [ -n "$CRON" ]; then
    echo "Running with cron schedule: $CRON"
    echo "$CRON python /app/kraken_to_ghostfolio.py" > /app/crontab
    exec supercronic /app/crontab
else
    echo "Running once (no CRON schedule set)"
    exec python /app/kraken_to_ghostfolio.py
fi
