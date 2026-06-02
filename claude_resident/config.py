"""Constants and configuration for the Claude resident harness."""

from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / "claude_state"
DEFAULT_MODEL = "us.anthropic.claude-opus-4-6-v1"

# Anthropic beta headers attached to every API call (via default_headers on
# the Bedrock client). Empty list = standard 200k window.
ANTHROPIC_BETAS: list[str] = []

MAX_CONTEXT_TOKENS = 180_000  # leave headroom in 200k window
MAX_RESPONSE_TOKENS = 16000   # raising further requires streaming (SDK rejects
                              # non-streaming whose estimated runtime > 10 min)
THINKING_BUDGET = 10000  # tokens for internal reasoning (not shown to users)
COOLDOWN_SECONDS = 3  # minimum gap between responses to avoid firehose behavior
MAX_TOOL_TURNS = 30  # maximum tool-calling iterations per response
SANDBOX_IMAGE = "claude-sandbox"
SANDBOX_TIMEOUT = 120  # seconds for compilation + execution
SANDBOX_MEMORY = "2g"
BG_CONTAINER_NAME = "claude-bg"
BG_CONTAINER_MEMORY = "8g"
SESSION_TTL = 300  # seconds — matches Anthropic cache TTL
SESSION_MAX_TOKENS = 150_000  # trim session before hitting 200k window
TOPIC_HISTORY_TOKEN_BUDGET = 20_000   # cold-start topic context
PERSON_NOTES_TOKEN_BUDGET = 8_000     # per-person notes

# Cross-stream context (e.g. #allgame loaded when responding elsewhere).
# Sliced per-topic instead of one stream-wide tail so every topic gets
# representation. Bounded three ways: msg count, per-topic tokens,
# total tokens.
STREAM_MESSAGES_PER_TOPIC = 75            # target msgs per topic
STREAM_PER_TOPIC_TOKEN_BUDGET = 4_000     # cap any one topic's slice
STREAM_HISTORY_TOKEN_BUDGET = 40_000      # overall cap (safety net)
SESSION_MAX_COUNT = 20  # max concurrent topic sessions in memory
SESSION_TRIM_PAIRS = 6  # when trimming, keep last N user/assistant pairs
MAX_STALE_FILES = 3  # reset session if more than this many state files diverged
TYPING_HOLDOFF_TIMEOUT = 30  # max seconds to wait for someone to finish typing

# Backfill — how much history to pull on boot
BACKFILL_PER_STANDING_STREAM = 200
BACKFILL_OTHER_STREAMS = 30         # for non-standing streams when enabled
BACKFILL_ALL_STREAMS = True         # pull a tail from every visible stream

# Metacognitive loop
METACOG_ENABLED = False  # daily self-curation runner — disabled to avoid
                         # unaccounted-for API spend; flip on when ready
METACOG_DEBOUNCE_SECONDS = 86400  # 24 hours — at most once per day
METACOG_CHECK_INTERVAL = 60  # seconds between background trigger checks
METACOG_MAX_TOOL_TURNS = 50  # more than conversation (30) — metacog does thorough curation

# Resolve relative to the package directory, not __file__
HARNESS_DIR = Path(__file__).resolve().parent.parent
HARNESS_PATH = HARNESS_DIR / "claude_resident.py"  # legacy shim location

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Streams where Claude has standing interest (will read ambient conversation).
DEFAULT_STANDING_STREAMS = [
    "allgame",       # the main stream — all topics within it
    "clankerville",  # the LLM/clanker neighborhood
    "gemmazone",     # Gemma's stream — Claude only responds to @-mentions here
]

# Streams where Claude only responds to direct @-mentions (no ambient judge).
# @-mentions and DMs still trigger regardless (see judge.py:78).
MENTION_ONLY_STREAMS = [
    "allgame",
    "gemmazone",
]
