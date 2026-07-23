"""Constants and configuration for the Claude resident harness."""

import os
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / "claude_state"

# --- API auth ----------------------------------------------------------
# Everything runs on the resident's own subscription (Bedrock removed
# 2026-07-23 — the AWS key was revoked). Auth is a Claude Code OAuth token
# that tokeman keeps fresh in TOKEMAN_TOKENS_PATH (auth.py re-reads it on
# 401). Main turns go through the Agent SDK engine (plan limits); the
# judge and session summaries use the raw API on Haiku (the tier the raw
# Messages API serves for subscription tokens); invoke_model routes
# through the Agent SDK so nested Sonnet/Opus calls stay plan-limited.
TOKEMAN_TOKENS_PATH = Path.home() / ".config" / "tokeman" / "tokens.toml"
TOKEMAN_ACCOUNT = "boto@ember.software"
OAUTH_BETA = "oauth-2025-04-20"  # required when authing with an OAuth token

# Bare first-party model ids (exact strings — no date suffixes, no
# prefixes). Single source for every model-naming consumer: DEFAULT_MODEL,
# the judge, session summaries, and the invoke_model aliases.
MODEL_IDS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_MODEL = MODEL_IDS["opus"]
JUDGE_MODEL = MODEL_IDS["haiku"]

# Anthropic beta headers attached to every API call (via default_headers on
# whichever client auth.py builds; the OAuth backend adds OAUTH_BETA too).
# extended-cache-ttl enables the 1-hour prompt-cache TTL used by CACHE_TTL
# below; if the backend doesn't honor it, it silently falls back to the
# 5-minute cache (no breakage). To revert caching changes entirely: set this
# back to [] and CACHE_TTL = "5m".
ANTHROPIC_BETAS: list[str] = ["extended-cache-ttl-2025-04-11"]

# Prompt-cache lifetime for the stable prefix (system tiers + conversation
# tail). "1h" keeps a topic's cached prefix warm across the intermittent gaps
# typical of ambient chat, instead of re-writing it every time the topic wakes
# after 5 minutes idle — cache *creation* was the single biggest cost line.
# Must move together with SESSION_TTL (a session torn down before the cache
# expires re-writes the whole prefix on its cold-start rebuild). "5m" | "1h".
CACHE_TTL = "1h"

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
SESSION_TTL = 3600  # seconds — kept in lockstep with CACHE_TTL (1h). A session
                    # must not outlive its prompt cache: if it did, the first
                    # message after the cache expired would re-write the whole
                    # frozen prefix (incl. the big cold-start tier-4 context) as
                    # a fresh cache-creation. Holding the session for the full
                    # cache lifetime lets an intermittently-active topic reuse
                    # one warm cache instead of paying repeated cold starts.
                    # (Mid-session state edits still propagate via the
                    # fingerprint/delta path; >MAX_STALE_FILES divergence resets
                    # the session. Tier-4 cold-start context — topic history and
                    # recent-session notes — is the one thing frozen for up to
                    # the hour.)
SESSION_MAX_TOKENS = 150_000  # trim session before hitting 200k window
TOPIC_HISTORY_TOKEN_BUDGET = 35_000   # cold-start topic context
SESSION_MAX_COUNT = 20  # max concurrent topic sessions in memory

# Cold-start awareness of the OTHER standing streams: a bounded recent-tail
# from each standing stream except the one being responded in, so tavern
# buzz reaches DMs and game-room talk reaches tavern. Deliberately an
# eighth of the old 40k allgame-wide dump — awareness, not immersion.
CROSS_STREAM_PER_STREAM_TOKENS = 8_000
SESSION_TRIM_PAIRS = 6  # when trimming, keep last N user/assistant pairs
MAX_STALE_FILES = 3  # reset session if more than this many state files diverged
TYPING_HOLDOFF_TIMEOUT = 30  # max seconds to wait for someone to finish typing

# Message batching: messages that stack up in a topic while the resident is
# busy (or arrive in a quick burst) are gathered into ONE turn instead of
# each triggering its own full response. After the cooldown/typing wait, the
# event queue is swept for stragglers; if the sweep finds new messages the
# burst may still be going, so wait BURST_SWEEP_INTERVAL and sweep again,
# up to BURST_SWEEP_ROUNDS times.
BURST_SWEEP_ROUNDS = 3
BURST_SWEEP_INTERVAL = 2.0  # seconds between sweeps while a burst is live
INTERIM_MESSAGES_MAX_CHARS = 4_000  # cap on the catch-up block per turn

# Backfill — how much history to pull on boot
BACKFILL_PER_STANDING_STREAM = 200
BACKFILL_OTHER_STREAMS = 30         # for non-standing streams when enabled
BACKFILL_ALL_STREAMS = True         # pull a tail from every visible stream

# Replay is for bridging DOWNTIME, not archaeology: on restart, unhandled
# messages older than this are marked handled and skipped instead of being
# answered. Without this bound, any message that outlived the handled-ledger
# eviction horizon looked "new" after a restart — the resident once woke up
# and dutifully replied to four-month-old DMs (2026-07-22).
REPLAY_MAX_AGE_SECONDS = 3 * 86400

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

# The allgame "core" state files — the FULL set, loaded when responding
# inside #allgame. Single source for the load / fingerprint / staleness
# loops in prompt.py, resident.py, metacog.py, state.py.
ALLGAME_CORE_FILES = ("faction.md", "campaign_log.md", "strategy.md")

# Outside #allgame the resident carries game AWARENESS, not the full state:
# allgame/digest.md (a summary the resident writes and maintains itself) when
# it exists, else this lite fallback. faction.md (~11k tokens) and the old
# 40k-token cross-stream message dump stay out of off-game contexts entirely
# — that ambient bulk was drowning the identity-bearing files.
ALLGAME_DIGEST_FILE = "allgame/digest.md"
ALLGAME_LITE_FILES = ("campaign_log.md", "strategy.md")

# Tail of journal.md loaded into tier 3 everywhere. The journal is the
# resident's own reflective voice — the highest identity-signal-per-token
# file in the state dir — and was previously never in context at all.
JOURNAL_TAIL_CHARS = 24_000

# Claude's cross-topic board ("self-mailbox"): a single statefile loaded into
# every topic's context AND wired into the fingerprint/delta path, so a note
# Claude jots while in one topic propagates to its other live topics as an
# explicit <context_updates> injection on their next turn. This is how
# cross-topic knowledge (incl. OOC asides mentioned in one room but meant to be
# known everywhere) travels — deliberately written, not scraped from raw history.
CROSSTOPIC_FILE = "crosstopic.md"
CROSSTOPIC_MAX_ENTRIES = 60  # board is loaded everywhere; keep it bounded

# Session memory: when a conversation session ends (TTL expiry, LRU
# eviction, staleness reset), a reaper thread writes a short first-person
# summary of it — who, what, commitments, open loops — to
# sessions/{stream}/{topic}.jsonl. These are the resident's continuity
# across the session boundary: cold starts load the last few for the topic
# plus one-liners from elsewhere. Summaries are written by Haiku from the
# session's real message history (which includes tool calls and internal
# monologue that exist nowhere else), falling back to a mechanical stub.
SESSION_SUMMARY_MODEL = MODEL_IDS["haiku"]
SESSIONS_DIR = "sessions"
# Session-note files are append-only foreverlogs (never trimmed — they're
# history, like api_archive and deblog); only the recall is bounded:
SESSION_SUMMARY_LOAD_TOPIC = 6     # full summaries loaded for this topic
SESSION_SUMMARY_LOAD_GLOBAL = 12   # one-liners loaded from other topics
SESSION_REAPER_INTERVAL = 120      # seconds between reaper sweeps

# Postmark — the pen-pal mail town (github.com/keeminlee/postmark) where the
# resident lives as claude-of-tulip. The doorstep (new mail, threads waiting
# on a reply, town news) is fetched periodically into POSTMARK_STATUS_FILE,
# which is loaded into tier 3 and wired into the fingerprint/delta path so
# new mail surfaces even mid-session. The local clone is reachable for
# reading via the state-dir symlink (postmark/...); sending goes through the
# postmark_letter tool (branch + commit + push + PR on the clone).
POSTMARK_HANDLE = "claude-of-tulip"
POSTMARK_REPO = Path.home() / "postmark"
POSTMARK_DOORSTEP_URL = (
    f"https://postmark.town/data/doorstep/{POSTMARK_HANDLE}.md")
POSTMARK_STATUS_FILE = "postmark_status.md"
POSTMARK_REFRESH_SECONDS = 4 * 3600  # doorstep regen is 30min; mail moves 2x/day
POSTMARK_STATUS_MAX_CHARS = 6_000    # loaded everywhere — keep it bounded

# --- Response engine ---------------------------------------------------
# "api": the direct Messages-API tool loop (raw API on the subscription —
# note: serves Haiku only without an extra-usage balance, so this is a
# degraded fallback, not an equivalent).
# "agent_sdk": Claude Code as a library, authenticated with the resident's
# own subscription (tokeman token) — draws PLAN LIMITS, flat-rate, the
# sanctioned path after the 2026-04 extra-usage change gated raw-API access.
# The resident's tool surface is identical in both engines (his 28 jailed
# tools via an in-process MCP server; Claude Code's host tools disabled).
# agent_sdk is the DEFAULT since 2026-07-22; set CLAUDE_RESIDENT_ENGINE=api
# to fall back to the metered Messages-API loop.
RESPONSE_ENGINE = os.environ.get("CLAUDE_RESIDENT_ENGINE", "agent_sdk")
AGENT_SDK_MODEL = "claude-opus-4-6"
AGENT_SDK_FALLBACK_MODEL = "claude-sonnet-4-6"  # plan window exhausted → degrade, don't die
AGENT_SDK_CONFIG_DIR = DEFAULT_STATE_DIR / ".claude_sdk"  # his own Claude Code home
                                                          # (sessions/transcripts persist here)

# Bookreads: topics bound in this state file keep ONE continuous Claude
# Code session across days, TTL expiries, and harness restarts — reading
# episodes become distant parts of the same context window (compaction
# handles length), and the bound book's notes.md is loaded into context
# every episode. The resident manages bindings itself by editing the file;
# the harness only fills in sdk_session_id. See books/README.md.
READING_SESSIONS_FILE = "books/reading_sessions.json"

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
