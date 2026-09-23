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

# Processes started within this many seconds of each other are one
# generation. A parent spawns its child in ~1s, so this needs to be a
# few seconds — NOT generous. At 90 it clustered a whole second launch
# into the first one and reported 'healthy' while six Claudes ran.
GEN_TOLERANCE=10

RESIDENTS=("claude_resident:.zuliprc:resident.log"
           "kimi_resident:yue-zuliprc:yue.log"
           "glm_resident::kestrel.log")

# Match the module EXACTLY. A bare "-m kimi_resident" also matches
# "-m kimi_resident.mcp_server" — the MCP subprocess Yue spawns for every
# turn — which made `status` report a second generation and cry wolf on
# each of her turns, and made `stop` kill a child it had no business
# naming. Anchor on a following space or end of string.
pids_for() { pgrep -f -- "-m $1([[:space:]]|\$)" 2>/dev/null; }

status() {
    local any=0
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"
        local pids; pids=$(pids_for "$pkg" | tr '\n' ' ')
        local n; n=$(echo "$pids" | wc -w | tr -d ' ')
        # Distinct GENERATIONS, not distinct timestamps. A supervisor
        # spawns its child a moment after itself, so a perfectly normal
        # launch straddles a second boundary and shows two times — which
        # the first version of this check reported as an orphan every
        # single restart. Cluster anything within GEN_TOLERANCE seconds.
        # python3 rather than awk: BSD awk has no strftime, and this needs
        # to work on the machine it runs on, not the one it was written for.
        local gens; gens=$(ps -o lstart= -p "${pids// /,}" 2>/dev/null \
            | python3 -c "
import sys, time, calendar
tol = $GEN_TOLERANCE
ts = sorted(calendar.timegm(time.strptime(l.strip(), '%a %b %d %H:%M:%S %Y'))
            for l in sys.stdin if l.strip())
out, base = [], None
for t in ts:
    if base is None or t - base > tol:
        base = t
        out.append(time.strftime('%H:%M:%S', time.gmtime(t)))
print(' '.join(out))
" 2>/dev/null)
        local ngen; ngen=$(echo "$gens" | wc -w | tr -d ' ')
        printf "  %-16s procs=%-3s generation(s): %s\n" "$pkg" "$n" "${gens:-none}"
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
    # Refuse to start on top of a running set. `start` used to launch
    # unconditionally, so calling it twice silently doubled every resident
    # — which is exactly the failure this script exists to prevent, and it
    # happened here while testing the script itself.
    local running=0
    for spec in "${RESIDENTS[@]}"; do
        local pkg="${spec%%:*}"
        running=$(( running + $(pids_for "$pkg" | wc -l) ))
    done
    if [ "$running" -gt 0 ]; then
        echo "  REFUSING to start: $running process(es) already running."
        echo "  Use 'restart', or 'stop' first."
        return 1
    fi
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
