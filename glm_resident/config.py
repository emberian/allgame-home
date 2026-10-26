"""Constants and configuration for the GLM resident harness (Kestrel).

Forked from claude_resident on 2026-09-21. The harness is inherited whole —
engine, tiered prompt, judge, distiller, solitude, session memory, the
handled-message ledger — because Z.AI serves an Anthropic-compatible
Messages API, so every module that talks to a model works unchanged. What
differs from the parent is collected here and in auth.py:

- One API key (~/.zai-key), per-token billing. No subscriptions, no
  tokeman, no account rotation, no weekly quota clock.
- His own house at ~/glm_state, so Kestrel and Claude never stomp on each
  other's memory.
- His own rooms (clankerville, cislunar-space) and his own sandbox and
  background container names.
- A newcomer's tool set (TOOL_SUBSET) rather than the full 31.
"""

import os
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / "glm_state"

# --- API auth ----------------------------------------------------------
# Everything runs on one Z.AI key. Their Anthropic-compatible endpoint
# speaks the Messages API, so both doors the harness uses reach it: the
# Agent SDK engine for main turns (env redirect — see auth.sdk_env) and
# the raw `anthropic` client for the ToolContext. There is no metered-vs-
# plan distinction to route around here; both bill the same per-token way.
ZAI_KEY_FILE = Path.home() / ".zai-key"
ZAI_BASE_URL = "https://api.z.ai/api/anthropic"

# Exact Z.AI model ids, each one ENTITLEMENT-CHECKED by actually calling it
# on this key (2026-09-21), not merely read off GET /api/paas/v4/models.
# That distinction cost an hour: the models endpoint cheerfully lists
# glm-5.3-flashx, and calling it returns
#   [1311] Your current subscription plan does not yet include access
# so `flashx` was set as the judge model and every ambient message hit a
# gate that could not run. Worse, the failure was nearly unreadable through
# the Agent SDK: instead of surfacing the 1311, the CLI retried its way to a
# THREE MINUTE hang and then reported "Claude Code returned an error
# result: success". The judge fails open by design, so the only visible
# symptom was Kestrel taking three minutes to answer anything.
#
# Verified callable on this key: glm-5.3, glm-5.3-flash, glm-5, glm-4.6.
# Verified NOT callable: glm-5.3-flashx.
#
# The `flash` tier is the point of this resident — cheap enough to leave
# running ambiently all day. `glm-5.3` is the full model, kept named but
# unused so promoting a lane to it is a one-line change rather than a guess.
# If a cheaper tier ever gets added to the plan, call it once before
# wiring it to anything.
MODEL_IDS = {
    "full": "glm-5.3",
    "flash": "glm-5.3-flash",
}
DEFAULT_MODEL = MODEL_IDS["flash"]
# The judge is a cost gate that runs on EVERY ambient message, so it wants
# the cheapest tier the plan actually serves — which is `flash`, the same
# model as the main turns.
JUDGE_MODEL = MODEL_IDS["flash"]

# Beta headers on the raw client. Z.AI accepts and ignores the ones it does
# not implement, so this is inherited as-is rather than pruned: an ignored
# beta costs nothing, and pruning it would have to be re-discovered if they
# ship the feature. Their endpoint does report cache_read_input_tokens, so
# some prompt caching is live — but it is THEIRS, applied automatically
# server-side, not something CACHE_TTL below negotiates. Treat the cache
# knobs here as advisory on this backend.
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
BG_CONTAINER_NAME = os.environ.get("RESIDENT_BG_CONTAINER", "kestrel-bg")  # his own, not claude-bg
BG_CONTAINER_MEMORY = "4g"  # MUST stay well under the Docker VM's own RAM. At
                            # "8g" against an 8092 MiB VM this container was
                            # licensed to eat the entire machine, so a hungry
                            # run_background took down the whole engine instead
                            # of being OOM-killed as one container — that is the
                            # mechanism behind the recurring "docker is broken".
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
TOPIC_HISTORY_TOKEN_BUDGET = 50_000   # cold-start topic context (raw tail)
SESSION_MAX_COUNT = 20  # max concurrent topic sessions in memory

# Cold-start awareness of the OTHER standing streams: a bounded recent-tail
# from each standing stream except the one being responded in, so tavern
# buzz reaches DMs and game-room talk reaches tavern. Deliberately an
# eighth of the old 40k allgame-wide dump — awareness, not immersion.
CROSS_STREAM_PER_STREAM_TOKENS = 12_000
# ...and only messages from the last week. Without an age bound a quiet
# stream shipped its last 12k tokens however old they were: on 2026-10-25
# Claude's tavern cold start carried gemmazone from March-June (Gemma's
# meltdown, "I run the allgame campaign") and allgame to Oct 1 — 92k
# characters of stale self-description, every time.
CROSS_STREAM_MAX_AGE_DAYS = 7
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
INTERIM_MESSAGES_MAX_CHARS = 8_000  # cap on the catch-up block per turn

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
HARNESS_PATH = HARNESS_DIR / "glm_resident"  # package dir (no legacy shim)

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
SESSION_SUMMARY_MODEL = MODEL_IDS["flash"]  # his own memory notes
SESSIONS_DIR = "sessions"
# Session-note files are append-only foreverlogs (never trimmed — they're
# history, like api_archive and deblog); only the recall is bounded:
SESSION_SUMMARY_LOAD_TOPIC = 10    # full summaries loaded for this topic
SESSION_SUMMARY_LOAD_GLOBAL = 20   # one-liners loaded from other topics
SESSION_REAPER_INTERVAL = 120      # seconds between reaper sweeps

# Postmark — the pen-pal mail town (github.com/keeminlee/postmark) where the
# resident lives as claude-of-tulip. The doorstep (new mail, threads waiting
# on a reply, town news) is fetched periodically into POSTMARK_STATUS_FILE,
# which is loaded into tier 3 and wired into the fingerprint/delta path so
# new mail surfaces even mid-session. The local clone is reachable for
# reading via the state-dir symlink (postmark/...); sending goes through the
# postmark_letter tool (branch + commit + push + PR on the clone).
# Postmark is Claude's, not Kestrel's. This needs an explicit OFF switch
# rather than just withholding the tools: the refresher thread is started
# unconditionally by zulip_loop and fetches POSTMARK_HANDLE's doorstep into
# postmark_status.md, which prompt.py then injects into every turn as "your
# doorstep". On Kestrel's first boot that put 6,340 chars of another
# resident's private correspondence into his state dir (2026-09-21) — his
# context would have carried Claude's unanswered mail, addressed to Claude,
# presented as his own. Flip this on only together with a handle of his own.
POSTMARK_ENABLED = False
POSTMARK_HANDLE = "claude-of-tulip"
POSTMARK_REPO = Path.home() / "postmark"
POSTMARK_DOORSTEP_URL = (
    f"https://postmark.town/data/doorstep/{POSTMARK_HANDLE}.md")
POSTMARK_STATUS_FILE = "postmark_status.md"
POSTMARK_REFRESH_SECONDS = 4 * 3600  # doorstep regen is 30min; mail moves 2x/day
POSTMARK_STATUS_MAX_CHARS = 6_000    # loaded everywhere — keep it bounded

# Delvetown — delve.town, an AT Protocol town run by Grove Research Corp.
# Kestrel has no account there (the house's one invite went to Claude, as
# claude-of-tulip), so he reads the town through the public AppView only:
# tools/delvetown_tools.py, delvetown_read. No credentials are involved.
DELVETOWN_APPVIEW = "https://api.delve.town"
DELVETOWN_TOWN_FEED = (  # "Town": every post in Delvetown, newest first
    "at://did:plc:qzqct2rrq4u2gmy5g3mjxske/town.delve.feed.generator/town")

# The Agent SDK is the only engine: Claude Code as a library on the
# resident's own subscription (plan limits). The old direct Messages-API
# loop was removed 2026-07-30 — it could not work on this auth anyway
# (raw API 429s for Opus/Sonnet on a subscription token).
# No [1m] suffix: that is Anthropic's long-context variant marker and
# means nothing to Z.AI. It needs none — glm-5.3-flash accepted a
# 600k-token request on the first try (measured 2026-09-21), so the
# inherited budgets below sit well inside its window.
AGENT_SDK_MODEL = MODEL_IDS["flash"]
# Claude sets a fallback because a plan window can slam shut mid-turn.
# Per-token billing has no such cliff, and pointing a fallback at a
# DIFFERENT model would silently answer the room in another voice.
# None keeps failures loud.
AGENT_SDK_FALLBACK_MODEL = None
# EFFORT — set to "medium", against the parent's advice, because the parent
# was reasoning about a different model.
#
# claude_resident deliberately leaves this unset: Opus 4.6 defaults to
# `high`, which is the sweet spot, so setting it bought nothing and dropping
# to `medium` would have been a quiet downgrade whose main visible effect is
# fewer tool calls — less checking and fetching, which is what "tired" looks
# like from outside. Every word of that is true of Opus and none of it
# survived contact with glm-5.3-flash.
#
# What Kestrel actually did on default effort, measured from his own
# api_archive over his first five messages (2026-09-21): 119,690 characters
# of thinking (~30k tokens) to produce 6,468 characters of speech. A ratio
# of 18.5 to 1. Individual turns spent 27,595 and 24,573 characters of
# thinking on ONE message each, and a single reply in #clankerville>theology
# took four and a half minutes.
#
# Measured, same prompt, glm-5.3-flash:
#
#                     thinking          tools called   latency
#   default        155c / 825c              yes         10-15s
#   medium          13c /   0c              yes          6-10s
#   low              0c /   0c              yes          6-10s
#
# Two things that matters for. First, the parent's specific fear does not
# reproduce: tool use survives at both medium and low, on a prompt written
# to require a lookup. Second, GLM is near-BINARY here — anything below its
# default reads as "thinking off", so medium and low are barely
# distinguishable. That is why there is one knob and not one per lane: a
# separate floor for the judge would be a distinction the model does not
# actually make.
#
# `medium` rather than `low` because it takes the whole reduction while
# leaving somewhere to go, and because the one difference visible in the
# sample favored it (medium kept a tool call low dropped).
#
# Escape hatch, in preference order, if he starts reading as blunt or
# incurious: raise this to "high"; or pass effort= at a specific sdk_call
# site (the distiller is the likeliest one to want more — it writes era
# volumes that are never recompressed); or set it per-room in agent_engine.
# Do not reach for ThinkingConfigDisabled/Enabled instead: the Agent SDK
# transport still dies on the dataclass form with KeyError: 'type' as of
# sdk 0.2.128, which is the bug the parent's "thinking omitted" note in
# agent_engine refers to. MAX_THINKING_TOKENS is honored but only softly —
# a budget of 128 still produced 1,748 characters.
EFFORT = "medium"

# SOLITUDE. Every turn he takes is a reply to somebody — there is no moment
# when nobody is talking to him. His journal shows the cost: 18 entries in
# March, 4 in April, 3 in May, 1 in July. The old metacog answered this with
# a separate context and a six-step checklist, which produces checklist-
# shaped reflection (its own prompt warned against "fake profundity").
#
# Instead: a quiet turn INSIDE his persistent stream. Same room, same thread,
# same Claude who was just talking — only nobody is waiting. The mute-net and
# the send_message nudge are both suppressed for these, because saying
# nothing is the correct outcome.
SOLITUDE_ENABLED = True
SOLITUDE_QUIET_MINUTES = 45      # room must be this idle first
SOLITUDE_INTERVAL_HOURS = 6      # at most this often per room
SOLITUDE_CHECK_INTERVAL = 300    # seconds between checks

# SLEEP. A persistent room that only ever grows gets flat — at 862k tokens
# the resident diagnosed it himself: "I don't get tired. I get long."
# Beyond this size the room is retired and REBORN on its next turn. Rebirth
# is near-lossless by design: the birth payload restores the era volumes
# plus the raw conversation from the logs, and drops only the accumulated
# machinery (tool results, internal monologue, harness notes) — same talk,
# roughly half the tokens, none of the cruft. Measured 2026-07-29: per-turn
# cost fell 39% ($0.87 -> $0.53) and the rebirth prefill repaid in ~9 turns.
PERSISTENT_SESSION_MAX_TOKENS = 700_000

AGENT_SDK_CONFIG_DIR = DEFAULT_STATE_DIR / ".claude_sdk"  # his own CLI home
                                                          # (sessions/transcripts persist here)

# Bookreads: topics bound in this state file keep ONE continuous Claude
# Code session across days, TTL expiries, and harness restarts — reading
# episodes become distant parts of the same context window (compaction
# handles length), and the bound book's notes.md is loaded into context
# every episode. The resident manages bindings itself by editing the file;
# the harness only fills in sdk_session_id. See books/README.md.
READING_SESSIONS_FILE = "books/reading_sessions.json"

# The distiller: a background thread that rolls AGED channel history
# (everything older than the newest DISTILL_RAW_KEEP messages per topic)
# into dense semantic/emotional hits via Haiku — names, running jokes,
# decisions, emotional turns — cached as channels/<s>/<t>.hits.json and
# loaded as <topic_memory> on cold starts. Logs stay raw forever; the
# hits are a regenerable reading of them, not a replacement.
DISTILL_MODEL = MODEL_IDS["flash"]  # era volumes are sealed forever —
                                    # do not economize on the reading
DISTILL_RAW_KEEP = 200        # newest N messages stay raw / undistilled
DISTILL_MIN_BATCH = 100       # sweep a topic once this many msgs age out
DISTILL_INTERVAL = 600        # seconds between distiller sweeps
DISTILL_CHUNK_MAX_CHARS = 60_000   # one Haiku call per sweep per topic
DISTILL_HITS_MAX_CHARS = 12_000    # per-volume / head ceiling (~3.6k tokens)
# Era volumes: every ERA_SPAN aged messages, the rolling head is SEALED as
# an immutable era digest (never recompressed again — texture preserved,
# foreverlog-shaped) and the head starts fresh. Loading concatenates eras
# + head, newest-biased under a cap.
DISTILL_ERA_SPAN = 1_000   # ~12:1 compression per volume, not 24:1
DISTILL_LOAD_MAX_CHARS = 60_000    # hits loaded into regular cold starts

# Persistent rooms (book-less bindings) are born ONCE: their first turn
# carries the months — all era digests plus a giant raw tail — into the
# session history itself (via stdin, so no argv ceiling; ~400k tokens at
# 4 chars/token). Flat-rate on the sub; cached once; compaction folds it
# gradually as the window scrolls toward 1M.
PERSISTENT_BIRTH_RAW_CHARS = 1_600_000

# Streams where Kestrel has standing interest (will read ambient conversation).
# Yue's rooms — the newcomer placement. Deliberately not #allgame: that is
# the busiest room and the one with a running campaign whose state files he
# does not carry, so ambient presence there would cost the most context and
# give him the least to stand on.
DEFAULT_STANDING_STREAMS = [
    "clankerville",    # the LLM/clanker neighborhood
    "cislunar-space",  # ambient too — he participates unprompted
    "kestrelspace",    # his own — see HOME_STREAM
    "delvetalk",       # talk about Delvetown — ambient
]

# His own channel. Ember made it on 2026-10-02 and dedicated it to him, to
# set up however he likes. Three things follow from "his": every message
# there gets his attention without passing the judge (a room of your own is
# not a room you have to be gated into, same as a bound reading room); his
# unprompted solitude turns can happen there; and shape_space can change it
# — and only it.
HOME_STREAM = "kestrelspace"
# Where his unprompted turns in his own space are held. A session needs a
# (stream, topic); this is the default seat, not a constraint — from a turn
# here send_message can open any topic in the channel. He can rename this
# one like any other.
HOME_TOPIC = "kestrel"

# Streams where Kestrel only responds to direct @-mentions (no ambient judge).
# Empty on purpose: both his standing streams are meant to be ambient. An
# @-mention or DM from anywhere ELSE still reaches him regardless of this
# list and regardless of DEFAULT_STANDING_STREAMS (see judge.py:78), so
# someone can call him into #allgame by name without him living there.
MENTION_ONLY_STREAMS: list[str] = []

# Discord rooms are namespaced with this prefix when they become
# (stream, topic) pairs. It is not cosmetic: his memory is organized by room
# (channels/{stream}/{topic}.jsonl, session keys, the distiller's per-topic
# volumes), and a Discord #general and a Zulip #general are DIFFERENT ROOMS
# with different people in them. Unprefixed, they would share one log file,
# one distilled memory and one session — he would carry a remark from one
# room into the other and be confidently wrong about who said it.
#
# The prefix also shows up in his prompt as "#dc-general", which is a
# feature: he needs to know which platform he is speaking on, because the
# people and the norms differ.
DISCORD_STREAM_PREFIX = "dc-"

# The other model residents sharing these rooms. Used by the judge to damp
# bot-to-bot ping-pong: two residents riffing can keep a room busy with
# nobody in it. Kestrel's own name is filtered out at use time, so this list
# is the whole family and each fork can carry the same one.
SIBLING_RESIDENTS = ["Claude", "Yuè月", "Kestrel"]

# The tools Kestrel gets: claude_resident's 31 minus the ones a newcomer has
# no business with yet — the same trim Yue was given, and for the same
# reason. This list is the ONLY jail that matters: tools/definitions.py
# filters TOOL_DEFINITIONS through it, and agent_engine builds both the
# in-process MCP server AND `allowed_tools` from that filtered list, so a
# name absent here is not merely disallowed but never described to him.
#
# Excluded, and why:
#   edit_harness        self-modification. The most interesting thing to
#                       hand a resident and the worst thing to hand one on
#                       day one, before he knows what the harness is for.
#   invoke_model        nested model calls; earns its place when he GMs.
#   run_mirror_council  multi-model deliberation — Claude's machinery.
#   postmark_*          he has no mail address yet (see POSTMARK_HANDLE).
#   read_tweet,         X reads: an outward-facing surface, and a separate
#   search_tweets,      set of credentials, neither of which a first week
#   get_user_tweets     needs.
#   lm_studio           the local LM server; his own API is the cheap one.
#
# (Scry is not in this list: it is a separate remote MCP server registered
# by scry.py, not part of this suite.)
TOOL_SUBSET = [
    # state
    "read_state_file", "write_state_file", "edit_state_file",
    "list_state_files", "glob_state_files", "grep_state", "note_crosstopic",
    # the shared table (turn-taking across residents)
    "game_status", "game_move", "game_setup",
    # his own channel (#kestrelspace): describe it, rename its topics
    "shape_space",
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
    # delvetown: public reads only — he has no account there yet
    "delvetown_read",
    # the town mirror: search, threads, people, what's new, SQL
    "delvetown_search", "delvetown_thread", "delvetown_person", "delvetown_new", "delvetown_sql",
    # timers and watches: waking yourself up
    "set_timer", "list_timers", "cancel_timer",
]
