#!/bin/bash
# residents.sh — start/stop/status the resident family, correctly.
#
# Why this exists: `pkill -f "python -m claude_resident"` does NOT kill the
# resident. It matches only the `uv run python -m claude_resident` wrapper,
# because the actual process runs as
#
#   /opt/homebrew/.../Python.app/Contents/MacOS/Python -m claude_resident
#
# with a CAPITAL P. pkill -f is case sensitive, so the child survives, gets
# reparented to init, and keeps running — still judging, still posting,
# still spending quota. Restarting that way ORPHANS a live resident and
# starts a second one beside it.
#
# On 2026-09-23 that had accumulated three generations at once (3 Claudes,
# 2 Kestrels, 2 Yues), which showed up as duplicate log lines and as a
# "restart" that appeared not to take effect — the new process was running
# the new code while an old one kept answering with the old.
#
# The stable thing to match is the module argument, `-m <pkg>_resident`,
# which appears in both the wrapper and the child.
#
# Usage:  scripts/residents.sh status | stop | start | restart

cd "$(dirname "$0")/.." || exit 1

RESIDENTS=("claude_resident:.zuliprc:resident.log"
           "kimi_resident:yue-zuliprc:yue.log"
           "glm_resident::kestrel.log")

pids_for() { pgrep -f -- "-m $1" 2>/dev/null; }

status() {
    local any=0
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"
        local pids; pids=$(pids_for "$pkg" | tr '\n' ' ')
        local n; n=$(echo "$pids" | wc -w | tr -d ' ')
        # Distinct start times = distinct generations. More than one means
        # an orphan survived a previous restart.
        local gens; gens=$(ps -o lstart= -p "${pids// /,}" 2>/dev/null \
                           | awk '{print $4}' | sort -u | tr '\n' ' ')
        printf "  %-16s procs=%-3s generations: %s\n" "$pkg" "$n" "${gens:-none}"
        # More than one distinct start time means an orphan survived a
        # previous restart and is still answering the room with old code.
        local ngen; ngen=$(echo "$gens" | wc -w | tr -d ' ')
        [ "$ngen" -gt 1 ] && { echo "    ^^ MULTIPLE GENERATIONS — orphaned resident still running"; any=1; }
    done
    return $any   # 0 = healthy, 1 = duplicate generations found
}

stop() {
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"
        pkill -TERM -f -- "-m $pkg" 2>/dev/null
    done
    sleep 3
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"
        pkill -KILL -f -- "-m $pkg" 2>/dev/null
    done
    sleep 1
    local left=0
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"
        left=$(( left + $(pids_for "$pkg" | wc -l) ))
    done
    echo "  stopped; $left process(es) remaining"
    return $(( left > 0 ))
}

start() {
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"; local rest="${spec#*:}"
        local rc="${rest%%:*}"; local log="${rest#*:}"
        if [ -n "$rc" ]; then
            nohup uv run python -m "$pkg" --zuliprc "$rc" >> "$log" 2>&1 &
        else
            nohup uv run python -m "$pkg" >> "$log" 2>&1 &
        fi
        echo "  started $pkg -> $log"
    done
}

case "${1:-status}" in
    status)  status ;;
    stop)    stop ;;
    start)   start; sleep 3; echo; status ;;
    restart) stop || { echo "  REFUSING to start: processes survived the stop"; exit 1; }
             start; sleep 3; echo; status ;;
    *) echo "usage: $0 status|stop|start|restart"; exit 1 ;;
esac
