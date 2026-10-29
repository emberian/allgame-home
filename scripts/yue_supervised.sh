#!/bin/bash
# yue_supervised.sh — run the kimi resident under a crash-restart loop.
#
# Backoff doubles per consecutive failure (10s, 20s, ... capped 10 min) and
# resets after any run that survived 5+ minutes — chronic crash loops give
# up after 8 consecutive fast failures so a broken tree doesn't spin forever.
#
# Usage: scripts/yue_supervised.sh [--stream ...] [--model ...]

cd "$(dirname "$0")/.." || exit 1

fails=0
while true; do
    started=$(date +%s)
    uv run python -m kimi_resident "$@" >> yue.log 2>&1
    rc=$?
    now=$(date +%s)
    echo "$(date '+%F %T') [supervisor] yue exited rc=$rc after $((now - started))s" >> yue.log

    if [ $((now - started)) -ge 300 ]; then
        fails=0
    else
        fails=$((fails + 1))
        if [ "$fails" -ge 8 ]; then
            echo "$(date '+%F %T') [supervisor] crash loop — giving up" >> yue.log
            exit 1
        fi
    fi
    sleep $(( fails * 10 > 600 ? 600 : fails * 10 ))
done
