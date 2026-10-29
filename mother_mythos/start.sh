#!/bin/bash
# Start MotherMythos: an interactive Claude Code session in a detached tmux
# session named `mm`, fed by the tulip channel (channel.py, via .mcp.json).
#
#   ./start.sh                    start her (a new conversation)
#   ./start.sh -c                 continue her most recent conversation here
#   ./start.sh --resume <id> ...  any other claude flags pass straight through
#   tmux attach -t mm             sit at her terminal (detach: ctrl-b d)
#   tmux kill-session -t mm       stop her
#
# ALWAYS start and resume her through this script, never with a bare `claude`
# in this directory. Two things only this script supplies, and the channel
# needs both: --dangerously-load-development-channels, without which Claude
# Code ignores the channel's events, and MM_CHANNEL=1, without which channel.py
# does not listen to Zulip at all. On 2026-10-02 a session resumed with
# `claude --resume …` ran for an hour deaf: tools worked, nothing arrived, and
# two pings were found only because she went looking.
#
# Each launch shows Claude Code's development-channel warning and waits there
# until someone at the terminal confirms it. Attach and answer it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION=mm
MODEL=claude-fable-5-1
TMUX_BIN=/opt/homebrew/bin/tmux
CLAUDE_BIN="$HOME/.local/bin/claude"

# Her environment is built from nothing, so that whatever launched this script
# (often another Claude Code session, with its own CLAUDE*/ANTHROPIC* variables)
# cannot leak into hers. MM_CHANNEL=1 is what tells channel.py that this is the
# session that listens.
clean_env=(
  env -i
  HOME="$HOME"
  USER="$(id -un)"
  LOGNAME="$(id -un)"
  SHELL=/bin/zsh
  LANG=en_US.UTF-8
  PATH="$HOME/.local/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  MM_CHANNEL=1
)

if [[ "${1:-}" == "--inside" ]]; then
  # Running in the tmux pane. Scrub again here: a tmux server that was already
  # up hands new sessions its own environment, not the one built below.
  shift
  status=0
  "${clean_env[@]}" TERM="${TERM:-tmux-256color}" TMUX="${TMUX:-}" TMUX_PANE="${TMUX_PANE:-}" \
    "$CLAUDE_BIN" -r 30f4121e-7b23-45b7-949a-8185672cf6fa --model "$MODEL" --dangerously-load-development-channels server:tulip "$@" || status=$?
  echo
  echo "[claude exited with status $status. Press enter to close this tmux session.]"
  read -r _
  exit "$status"
fi

if "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already exists; not starting a second MotherMythos." >&2
  echo "  attach: tmux attach -t $SESSION      stop: tmux kill-session -t $SESSION" >&2
  exit 1
fi

# tmux takes the pane command as one shell string, so each pass-through
# argument is quoted for the shell before it goes in.
inside="$HERE/start.sh --inside"
for arg in "$@"; do inside+=" $(printf '%q' "$arg")"; done

"${clean_env[@]}" TERM="${TERM:-xterm-256color}" \
  "$TMUX_BIN" new-session -d -s "$SESSION" -x 200 -y 50 -c "$HERE" "$inside"

echo "MotherMythos is starting in tmux session '$SESSION'."
echo "  attach: tmux attach -t $SESSION"
