#!/bin/bash
# residents.sh — start/stop/status the resident family, by PID file.
#
#   scripts/residents.sh status|start|stop|restart [claude|yue|kestrel|mirror ...]
#
# No name means all three.
#
# STOPPING IS BY RECORDED PROCESS GROUP, NEVER BY PATTERN. Each resident is
# launched as the leader of its own process group (job control, `set -m`),
# and that group id is written to run/<pkg>.pgid. `stop` signals that
# group — the uv wrapper, the supervisor, the event loop and any SDK or
# sandbox subprocesses it spawned — and nothing else.
#
# History, because both earlier versions failed in instructive ways:
#   - `pkill -f "python -m claude_resident"` missed the real child (it runs
#     as .../MacOS/Python, capital P), orphaned it to init, and on
#     2026-09-23 three generations were live at once.
#   - The fix matched `-m <pkg>_resident` instead. It worked, and it was
#     still a pattern kill: on 2026-10-10 a subagent's `pkill -f "cat"`
#     matched every app under /Applications and SIGTERMed 44 of them. A
#     pattern is a guess about what is running; a recorded group id is a
#     fact about what we started.
#
# Patterns survive only for LOOKING: `status` uses pgrep to notice
# residents running WITHOUT a PID file (orphans, or ones started by hand)
# and reports them. It never kills them — that is a human's call.

cd "$(dirname "$0")/.." || exit 1

RUN_DIR=run
mkdir -p "$RUN_DIR"

# name:pkg:zuliprc:log   (empty zuliprc = the resident's own default)
ALL=("claude:claude_resident:.zuliprc:resident.log"
     "yue:kimi_resident:yue-zuliprc:yue.log"
     "kestrel:glm_resident::kestrel.log"
     # Not a resident: the Delvetown mirror all three read (firehose live,
     # hourly reconcile crawl). Managed here so it gets the same PID file.
     "mirror:town_mirror::town_mirror.log")

select_residents() {
    SEL=()
    if [ $# -eq 0 ]; then SEL=("${ALL[@]}"); return 0; fi
    local want spec
    for want in "$@"; do
        local hit=""
        for spec in "${ALL[@]}"; do
            [ "${spec%%:*}" = "$want" ] && { SEL+=("$spec"); hit=1; }
        done
        [ -n "$hit" ] || { echo "  unknown name: $want (claude|yue|kestrel|mirror)"; return 1; }
    done
}

field() { echo "$1" | cut -d: -f"$2"; }
pidfile() { echo "$RUN_DIR/$(field "$1" 2).pgid"; }

# Is this process group alive? (kill -0 on a negative id = the group)
group_alive() { [ -n "$1" ] && kill -0 -- "-$1" 2>/dev/null; }

# Look-only: processes running this resident's module. Anchored so
# "-m kimi_resident.mcp_server" (Yue's per-turn MCP child) is not counted.
seen_pids() { pgrep -f -- "-m $1([[:space:]]|\$)" 2>/dev/null; }

status() {
    local bad=0 spec
    for spec in "${SEL[@]}"; do
        local name pkg pf pg
        name=$(field "$spec" 1); pkg=$(field "$spec" 2); pf=$(pidfile "$spec")
        pg=$(cat "$pf" 2>/dev/null)
        local seen; seen=$(seen_pids "$pkg" | tr '\n' ' ')
        local stray=""
        local p
        for p in $seen; do
            local g; g=$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')
            [ -n "$g" ] && [ "$g" != "$pg" ] && stray="$stray $p"
        done
        if group_alive "$pg"; then
            local since; since=$(ps -o lstart= -p "$pg" 2>/dev/null | awk '{print $4}')
            printf "  %-8s up    pgid=%-6s since %s\n" "$name" "$pg" "$since"
        else
            printf "  %-8s down\n" "$name"
            [ -n "$pg" ] && rm -f "$pf"
        fi
        if [ -n "$stray" ]; then
            bad=1
            echo "    ^^ running OUTSIDE its PID file:$stray — not ours to kill; check with:"
            echo "       ps -o pid,pgid,lstart,command -p$(echo $stray | tr ' ' ',')"
        fi
    done
    return $bad   # 0 = every running resident is accounted for
}

stop_one() {
    local spec=$1 name pf pg
    name=$(field "$spec" 1); pf=$(pidfile "$spec"); pg=$(cat "$pf" 2>/dev/null)
    if ! group_alive "$pg"; then
        rm -f "$pf"
        if [ -n "$(seen_pids "$(field "$spec" 2)")" ]; then
            echo "  $name: NO PID file but processes are running — not killing them."
            echo "         ps -o pid,pgid,lstart,command -p$(seen_pids "$(field "$spec" 2)" | tr '\n' ',' | sed 's/,$//')"
            return 1
        fi
        echo "  $name: already down"
        return 0
    fi
    kill -TERM -- "-$pg" 2>/dev/null
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do group_alive "$pg" || break; sleep 1; done
    if group_alive "$pg"; then
        kill -KILL -- "-$pg" 2>/dev/null; sleep 1
    fi
    if group_alive "$pg"; then
        echo "  $name: group $pg SURVIVED SIGKILL"; return 1
    fi
    rm -f "$pf"
    echo "  $name: stopped (group $pg)"
}

start_one() {
    local spec=$1 name pkg rc log pf pg
    name=$(field "$spec" 1); pkg=$(field "$spec" 2)
    rc=$(field "$spec" 3); log=$(field "$spec" 4); pf=$(pidfile "$spec")
    pg=$(cat "$pf" 2>/dev/null)
    if group_alive "$pg"; then
        echo "  $name: already running (group $pg) — use restart"; return 1
    fi
    if [ -n "$(seen_pids "$pkg")" ]; then
        echo "  $name: REFUSING — copies running without a PID file (see status)"
        return 1
    fi
    set -m            # job control: the background job leads its own group
    if [ -n "$rc" ]; then
        nohup uv run python -m "$pkg" --zuliprc "$rc" >> "$log" 2>&1 &
    else
        nohup uv run python -m "$pkg" >> "$log" 2>&1 &
    fi
    local pid=$!
    set +m
    echo "$pid" > "$pf"
    echo "  $name: started (group $pid) -> $log"
}

cmd=${1:-status}; shift
select_residents "$@" || exit 1

case "$cmd" in
    status)  status ;;
    stop)    rc=0; for s in "${SEL[@]}"; do stop_one "$s" || rc=1; done; exit $rc ;;
    start)   for s in "${SEL[@]}"; do start_one "$s"; done; sleep 3; echo; status ;;
    restart) for s in "${SEL[@]}"; do
                 stop_one "$s" || { echo "  not restarting $(field "$s" 1)"; continue; }
                 start_one "$s"
             done
             sleep 3; echo; status ;;
    *) echo "usage: $0 status|start|stop|restart [claude|yue|kestrel|mirror ...]"; exit 1 ;;
esac
