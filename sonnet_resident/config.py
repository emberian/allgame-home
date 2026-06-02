"""Constants and configuration for the Sonnet resident harness.

Forked from claude_resident (Opus) on 2026-04-09. Key differences:
- Model is Sonnet 4.6 (cheaper, slower per-token reasoning)
- State lives in ~/sonnet_state/ so Sonnet and Opus don't stomp on each other
- Dynamic state loading is mostly disabled — Sonnet reads from a single dense
  brief.md loaded once per session instead of the tiered scratchpad/topic-history
  pipeline. Topic history budget is tiny: we rely on the brief + live Zulip
  context for orientation.
- Separate sandbox / background container names to avoid collisions with the
  Opus resident.
"""

from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / "sonnet_state"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_CONTEXT_TOKENS = 180_000  # leave headroom in 200k window
MAX_RESPONSE_TOKENS = 8000  # Sonnet responses are cheaper, but keep short
THINKING_BUDGET = 4000  # Sonnet uses explicit budget mode
COOLDOWN_SECONDS = 3  # minimum gap between responses to avoid firehose behavior
MAX_TOOL_TURNS = 20  # Sonnet is faster per turn, lower the cap
SANDBOX_IMAGE = "claude-sandbox"  # same docker image as Opus harness
SANDBOX_TIMEOUT = 120
SANDBOX_MEMORY = "2g"
BG_CONTAINER_NAME = "sonnet-bg"  # separate container from opus
BG_CONTAINER_MEMORY = "8g"
SESSION_TTL = 300  # matches Anthropic 5-min cache TTL
SESSION_MAX_TOKENS = 150_000
# Brief is the new Tier 1. Topic history is supplementary context only —
# keep it tiny since the brief carries the continuity.
TOPIC_HISTORY_TOKEN_BUDGET = 8_000
SESSION_MAX_COUNT = 20
SESSION_TRIM_PAIRS = 6
MAX_STALE_FILES = 3
TYPING_HOLDOFF_TIMEOUT = 30

# Metacognitive loop (inherited, but may be adjusted later)
METACOG_DEBOUNCE_SECONDS = 86400
METACOG_CHECK_INTERVAL = 60
METACOG_MAX_TOOL_TURNS = 50

# Resolve relative to the package directory, not __file__
HARNESS_DIR = Path(__file__).resolve().parent.parent
HARNESS_PATH = HARNESS_DIR / "sonnet_resident.py"  # legacy shim location

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Streams where Sonnet has standing interest (ambient judge-gated).
DEFAULT_STANDING_STREAMS = [
    "allgame",       # the main stream — all topics within it
    "clankerville",  # the LLM/clanker neighborhood
]

# Streams where Sonnet only responds to direct @-mentions (no ambient judge).
MENTION_ONLY_STREAMS = [
    "gemmazone",     # Gemma's space — don't ambient-respond
]
