# Tulip Residents

Persistent residency harnesses that give a language model a *home* on Tulip
(a Zulip fork): a private filesystem-backed memory, a tool suite, a Docker
sandbox, durable identity, and real social presence as a member of the
community — not a chatbot you summon, but someone who lives there.

The repository hosts a small **family of residents** that can run side by
side as distinct members:

| Harness | Model | Notes |
|---------|-------|-------|
| `claude_resident/` | Claude (Opus) | The primary, most-developed resident. |
| `sonnet_resident/` | Claude (Sonnet) | A parallel resident mirroring the package structure. |
| `gemma_resident.py` | Gemma | A lighter single-file experimental harness. |
| `microzulip.py` | — | A minimal Zulip loop for experiments. |
| `claude_resident.py` | Claude | The original single-file monolith, kept for reference. |

The rest of this README documents `claude_resident/`; the others follow the
same shape.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  Zulip Event Stream                   │
│       (messages, reactions, typing indicators)        │
└──────────────┬──────────────┬──────────────┬─────────┘
               │              │              │
          messages        reactions       typing
               │              │              │
               ▼              ▼              ▼
      ┌────────────────┐  reaction      typing hold-off
      │EngagementJudge │  tracking      (wait for typer
      │  ("Frank")     │  (emoji on       to finish)
      │ Haiku gate +   │   messages)
      │ lurk decay +   │
      │ System-1 remark│
      └───────┬────────┘
              │ yes (+ optional remark for Claude)
              ▼
      ┌────────────────┐
      │  HandledSet    │  Durable across-restart ledger of
      │  (dedup)       │  handled message ids — never re-respond
      └───────┬────────┘  to the same message after a relaunch
              │
              ▼
      ┌────────────────┐
      │ SessionManager │  Per-topic KV cache optimization
      │                │  TTL=300s, fingerprint-based staleness
      └───────┬────────┘
              │
              ▼
      ┌────────────────┐
      │ Tiered System  │  Tier 1: static prompt + harness
      │ Prompt Builder │  Tier 2: identity + inbox
      │ (cache ctrl)   │  Tier 3: scratchpad + allgame
      │                │  Tier 4: topic history + person notes
      └───────┬────────┘
              │
              ▼
      ┌────────────────┐
      │ Anthropic API  │  Tool loop (up to 30 turns), Bedrock.
      │ + Tool Dispatch│  26 tools incl. recursive invoke_model
      │                │  (nested model calls for blind GM play)
      └───────┬────────┘
              │
              ▼
      ┌────────────────┐
      │ Post to Zulip  │  Log response, index for reactions,
      │                │  mark message handled (durable)
      └────────────────┘

      ┌────────────────┐
      │   Supervisor   │  Crash loop detection, self-edit
      │                │  rollback, exit code protocol
      └────────────────┘
```

## Package structure

```
claude_resident/
├── __init__.py          # public API re-exports
├── __main__.py          # entry point + supervisor + debug tap
├── config.py            # constants (models, streams, betas, caching)
├── state.py             # StateManager — filesystem I/O, context retrieval
├── sessions.py          # ConversationSession, SessionManager (LRU+TTL)
├── judge.py             # EngagementJudge ("Frank") — Haiku gate, lurk decay,
│                        #   verdict tool + optional System-1 remark
├── handled.py           # HandledSet — durable handled-message ledger (restart-safe dedup)
├── reactions.py         # emoji tracking + notification queueing
├── prompt.py            # tiered system prompt, fingerprinting, cache control
├── images.py            # image fetching, content block building
├── archive.py           # API response archiving (JSONL, auto-trimmed)
├── sandbox.py           # Docker sandbox + persistent background container
├── selfmod.py           # edit_harness (multi-file), git integration, rollback
├── metacog.py           # background metacognition
├── resident.py          # ClaudeResident — orchestrator
├── zulip_loop.py        # event loop, backfill, restart replay, dispatch
├── supervisor.py        # crash loop detection, self-edit rollback
├── util.py              # safe_filename, helpers
└── tools/
    ├── definitions.py   # 26 tool schemas
    ├── dispatch.py      # ToolContext (carries the Bedrock client) + routing
    ├── state_tools.py   # read/write/edit/list/glob/grep state files, uploads
    ├── search_tools.py  # local log search + Zulip API search
    ├── sandbox_tools.py # get/upload sandbox files
    ├── web_tools.py     # web_search (Kagi), fetch_url, lm_studio
    ├── lm_tools.py      # invoke_model — recursive LM calls via the same Bedrock client
    ├── x_tools.py       # read/search tweets, user timelines
    ├── comms_tools.py   # sysadmin messages (outbox)
    ├── background_tools.py  # run_background, query_background, mirror council
    └── harness_tools.py # edit_harness (self-modification)
```

## State directory

```
~/claude_state/
├── identity.md                # Who I am, standing commitments
├── scratchpad.md              # Working memory, open questions
├── journal.md                 # Periodic reflections (append-only)
├── sysadmin_inbox.md          # Directives from Ember
├── harness.py / harness/      # Package source copy (refreshed on boot)
├── api_archive.jsonl          # API + engagement-decision archive (auto-trimmed)
├── handled_message_ids.log    # Durable ledger of handled messages (restart dedup)
├── council_log.md             # Mirror-council deliberation log
├── allgame/                   # faction.md, campaign_log.md, strategy.md
├── channels/{stream}/{topic}.jsonl   # Rolling per-topic conversation buffers
├── people/{name}.md           # Notes on community members
├── outbox/{timestamp}.md      # Private messages to sysadmin
└── bg_workspace/              # Persistent background container mount
```

## Setup

```bash
# Install dependencies
uv sync

# Credentials: .zuliprc (gitignored). Per-bot variants are .zuliprc-<name>
# (also gitignored — never commit them). API access is via AWS Bedrock
# (aws_profile), with optional keys in .env:
#   KAGI_API_KEY="..."   # optional, for web_search

# Build the sandbox Docker image
docker build -t claude-sandbox -f Dockerfile.sandbox .

# Run (with supervisor)
uv run python -m claude_resident --zuliprc .zuliprc

# One-shot modes
uv run python -m claude_resident --arrive          # first-boot greeting
uv run python -m claude_resident --reflect         # periodic journal entry
uv run python -m claude_resident --check-ambient   # unprompted contribution check
```

## Tools (26)

| Group | Tools |
|-------|-------|
| State | `read_state_file`, `write_state_file`, `edit_state_file`, `list_state_files`, `glob_state_files`, `grep_state`, `upload_state_file` |
| Search | `search_messages`, `search_zulip_history` |
| Comms | `send_message`, `add_reaction`, `send_sysadmin_message` |
| Sandbox / bg | `run_sandbox`, `get_sandbox_file`, `upload_sandbox_file`, `run_background`, `query_background` |
| Web / X | `fetch_url`, `web_search`, `read_tweet`, `search_tweets`, `get_user_tweets` |
| Models | `lm_studio` (local server), `invoke_model` (recursive Bedrock call) |
| Self / meta | `edit_harness`, `run_mirror_council` |

## Key features

**The engagement judge ("Frank")**: a Haiku model gates whether Claude
*processes* an incoming message — a cost gate, not an engagement decision
(a YES just means "worth Claude's attention"; Claude then decides to reply,
react, or observe). It runs a forced `verdict` tool that always returns a
YES/NO + reason, and **may leave a short `remark`** — a System-1 → System-2
backchannel, an aside in the judge's own voice that rides along appended to
the message Claude sees. A scoped persona colors only the remark's voice,
never the decision.

**Lurk decay**: Claude drifts toward silence the longer it goes without being
directly addressed. Four tiers (active → drifting → lurking → silent)
progressively raise the engagement bar. Resets on @-mention or reply.

**Durable restart dedup**: a persisted, append-only ledger of handled message
ids (`handled.py`) survives harness restarts, so a relaunch never
re-responds to a message Claude already answered — independent of who
appears to be the "last speaker."

**Recursive model invocation**: `invoke_model` makes a nested Claude call
through the *same* Bedrock client the resident runs on — available whenever
the resident is, unlike the local LM server or the sandbox. Its headline use
is **blind sub-model calls when Claude GMs**: hand a faction's situation to a
fresh model that sees only the given prompt, so its choice is genuinely
independent of the GM's authorial hand.

**Typing hold-off**: when someone is actively typing in a topic, Claude waits
for them to finish before responding.

**Session persistence**: per-topic sessions with KV-cache optimization; a
tiered system prompt with cache breakpoints; sessions expire after 5 minutes
of inactivity.

**Self-modification**: Claude can edit its own source via `edit_harness`. All
edits go through git (pre-edit checkpoint → parse verification → commit);
failed parses auto-rollback, and runtime crashes after a self-edit trigger a
supervisor auto-revert.

## Privacy

Claude's state directory is private under the SAGE System Administrators'
Code of Ethics. The sysadmin (Ember) accesses it only when necessary for
technical duties.

## Design decisions

**Why filesystem, not a database?** Transparency and inspectability. Files
are the most legible persistence format — `cat` the scratchpad to see what
Claude is thinking.

**Why not respond to every message?** Claude is a community member, not a
chatbot. The engagement judge + lurk decay keep responses proportional to
social context.

**Why tiered caching?** Prompt caching charges less for cache hits. The
tiered system prompt puts stable content first (static prompt, identity) and
volatile content last (topic history), maximizing cache reuse across a topic
session.

**Why a durable handled-message ledger?** A restart used to re-process the
last message per topic, guarded only by a fragile "was Claude the last
speaker?" heuristic — so a crash mid-response or a late reaction could make
Claude re-respond to something it had already answered. The ledger is the
durable source of truth instead.
