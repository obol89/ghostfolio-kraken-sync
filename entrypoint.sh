#!/bin/bash
set -e

# SYNC_ARGS is intentionally unquoted so it word-splits into separate flags.
# It is the only way a containerised run can reach a command line flag such as
# --reconcile or --dry-run; leaving it unset keeps the default sync behaviour.
if [ -n "$CRON" ]; then
    echo "Running with cron schedule: $CRON"
    echo "$CRON python /app/kraken_to_ghostfolio.py $SYNC_ARGS" > /app/crontab
    exec supercronic /app/crontab
else
    echo "Running once (no CRON schedule set)"
    exec python /app/kraken_to_ghostfolio.py $SYNC_ARGS
fi
