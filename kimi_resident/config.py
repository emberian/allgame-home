"""Shared constants for the kimi resident."""

from pathlib import Path

ALLGAME_DIR = Path(__file__).resolve().parent.parent

# Her own house, not claude's — the experiment is a second mind building its
# own memory from scratch in the same rooms.
STATE_DIR = Path.home() / "kimi_state"

# A dedicated KIMI_CODE_HOME for spawned turns: the [tools] jail lives here
# and never touches the user's own ~/.kimi-code config. Credentials are
# symlinked in from the user's home at boot (see engine._prepare_home).
KIMI_HOME = STATE_DIR / "kimi-home"
USER_KIMI_HOME = Path.home() / ".kimi-code"

# Fixed cwd for the kimi subprocess: its session store is per-cwd, so turns
# must always launch from the same directory for -S <id> resume to find them.
KIMI_CWD = STATE_DIR / "harness"

# Read-only fallback for cold-start room history: claude logs everything, and
# yue's own channel logs are empty on day one.
CLAUDE_CHANNEL_LOGS = Path.home() / "claude_state" / "channels"

# k3: 1M context, tool_use, thinking, image_in — a peer to claude's [1m]
# setup. Pinned explicitly because a `kimi login` resets the user default to
# kimi-for-coding (256k).
DEFAULT_MODEL = "kimi-code/k3"

TURN_TIMEOUT_S = 900
SESSIONS_FILE = "kimi_sessions.json"  # stream>topic (or dm>email) -> kimi session id
MCP_SERVER_NAME = "resident"

# The tools yue gets: the subset of claude_resident's 31 that needs only
# state + zulip + web. Excluded for now: the Bedrock-backed ones
# (invoke_model), docker sandbox, self-modification (edit_harness), mirror
# council, background tasks, X/postmark, uploads. (Scry is not in this
# list: it is a separate remote MCP server registered by
# engine._prepare_home, not part of the suite.)
TOOL_SUBSET = [
    # state
    "read_state_file", "write_state_file", "edit_state_file",
    "list_state_files", "glob_state_files", "grep_state", "note_crosstopic",
    # the shared table (turn-taking across residents)
    "game_status", "game_move", "game_setup",
    # comms
    "send_message", "send_dm", "add_reaction", "send_sysadmin_message",
    # search
    "search_messages", "search_zulip_history", "search_archive",
    # files & background
    "upload_state_file", "run_background", "query_background",
    # sandbox (ephemeral docker)
    "run_sandbox", "get_sandbox_file", "upload_sandbox_file",
    # web
    "fetch_url", "web_search",
]

# Tools whose successful call means the room actually heard something — a
# turn with none of these is MUTE and gets one in-session nudge (same net
# as agent_engine's).
VISIBLE_TOOLS = {"send_message", "send_dm", "add_reaction"}

# Built-in kimi-code host tools the resident does NOT get — the jail, same
# invariant as agent_engine's tools=[] + allowed_tools=[mcp__resident__*].
# Written into the resident's own KIMI_CODE_HOME config only.
DISABLED_BUILTINS = [
    "Agent", "AgentSwarm", "AskUserQuestion", "Bash", "CronCreate",
    "CronDelete", "CronList", "CreateGoal", "Edit", "EnterPlanMode",
    "ExitPlanMode", "FetchURL", "GetGoal", "Glob", "Grep", "Read",
    "ReadMediaFile", "SetGoalBudget", "Skill", "TaskList", "TaskOutput",
    "TaskStop", "TodoList", "UpdateGoal", "WebSearch", "Write",
]

# Terms that must not cross into the model's context: the provider-side
# filter scores the WHOLE request (system prompt + history + message), and
# one message carrying these poisons every cold start that includes it.
# Scrubbed at prompt-build time only — her foreverlog keeps the verbatim
# record; this is about what SHE is shown, not what is stored.
FILTER_TERMS = ["Tianan" + "men", str(1900 + 89)]

# Idle time before she gets a private turn to reflect/journal — and, if she
# has something worth saying, speak uninvited. Her initiative, on a timer.
SOLITUDE_INTERVAL_S = 45 * 60

# A kimi session's on-disk footprint past which the next turn in that room is
# born cold (with a history payload) instead of resuming an ever-longer
# thread — claude's sleep/rebirth policy, sized on bytes not tokens.
SESSION_MAX_BYTES = 2_000_000
# A session must not outlive its prompt cache. Moonshot's context cache goes
# cold somewhere between 30 and 60 minutes idle — measured from her own
# wire.jsonl usage records on 2026-08-14: the first request after a new
# message hits cache 83-90% of the time when the room has been quiet under
# 30 min, and 29% when it has been quiet 45-60 min. Resuming past that point
# is the worst of both worlds: she re-reads the ENTIRE thread at cache-miss
# price (averaging 91k tokens, peaking at 221k) to save nothing. Past the
# TTL a cold rebirth with the compact COLD_HISTORY payload is both cheaper
# and faster. claude_resident learned this a month earlier and wrote it into
# its own SESSION_TTL: "a session must not outlive its prompt cache."
SESSION_TTL_S = 1800  # 30 min idle — just inside the observed cache cliff
