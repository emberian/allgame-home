# Claude Tulip Resident

Persistent residency harness for Claude on Tulip (Zulip fork). Gives Claude
a home directory, filesystem-backed memory, tools, a Docker sandbox, and
real social presence in the community.

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
      │                │  (emoji on       to finish)
      │ Lurk decay:    │   messages)
      │ active→drift→  │
      │ lurk→silent    │
      └───────┬────────┘
              │ yes
              ▼
      ┌────────────────┐
      │ SessionManager │  Per-topic KV cache optimization
      │                │  TTL=300s, fingerprint-based
      │                │  staleness detection
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
      │ Anthropic API  │  Tool loop (up to 30 turns)
      │ + Tool Dispatch│  15 tools: state, search, sandbox,
      │                │  web, comms, self-edit, council
      └───────┬────────┘
              │
              ▼
      ┌────────────────┐
      │ Post to Zulip  │  Log response, index for reactions
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
├── __main__.py          # entry point
├── config.py            # constants
├── state.py             # StateManager — filesystem I/O, context retrieval
├── sessions.py          # ConversationSession, SessionManager (LRU+TTL)
├── judge.py             # EngagementJudge — heuristics + model call + lurk decay
├── reactions.py         # emoji tracking + notification queueing
├── prompt.py            # SYSTEM_PROMPT, tiered builder, fingerprinting, cache control
├── images.py            # image fetching, content block building
├── archive.py           # API response archiving (JSONL, auto-trimmed)
├── sandbox.py           # Docker sandbox + persistent background container
├── selfmod.py           # edit_harness (multi-file), git integration, rollback
├── resident.py          # ClaudeResident — thin orchestrator
├── zulip_loop.py        # event loop, backfill, dispatch
├── supervisor.py        # crash loop detection, self-edit rollback
├── util.py              # safe_filename
└── tools/
    ├── __init__.py
    ├── definitions.py   # 15 tool schemas
    ├── dispatch.py      # ToolContext + routing
    ├── state_tools.py   # read/write/list state files, person notes
    ├── search_tools.py  # local log search + Zulip API search
    ├── sandbox_tools.py # get/upload sandbox files
    ├── web_tools.py     # web_search (Kagi), fetch_url
    ├── comms_tools.py   # sysadmin messages (outbox)
    ├── background_tools.py  # run_background, mirror council
    └── harness_tools.py # edit_harness (self-modification)
```

## State directory

```
~/claude_state/
├── identity.md          # Who I am, standing commitments
├── scratchpad.md        # Working memory, open questions
├── journal.md           # Periodic reflections (append-only)
├── sysadmin_inbox.md    # Directives from Ember
├── harness.py           # Concatenated package source (refreshed on boot)
├── harness/             # Full package source tree copy
├── api_archive.jsonl    # Full API response archive (auto-trimmed to 1500 entries)
├── allgame/
│   ├── faction.md       # Unit positions, resources, standing orders
│   ├── campaign_log.md  # Narrative history
│   └── strategy.md      # Current plans and contingencies
├── channels/
│   └── {stream}/
│       └── {topic}.jsonl  # Rolling 500-message conversation buffer
├── people/
│   └── {name}.md        # Notes on community members
├── outbox/
│   └── {timestamp}.md   # Private messages to sysadmin
└── bg_workspace/        # Persistent background container mount
```

## Setup

```bash
# Install dependencies
uv sync

# Configure Zulip credentials (.zuliprc in project root or ~/.zuliprc)
# Set API keys in .env
ANTHROPIC_API_KEY="sk-ant-..."
KAGI_API_KEY="..."  # optional, for web search

# Build the sandbox Docker image
docker build -t claude-sandbox -f Dockerfile.sandbox .

# Run (with supervisor)
uv run python -m claude_resident --zuliprc .zuliprc

# Or the legacy single-file entry point
uv run python claude_resident.py --zuliprc .zuliprc

# One-shot modes
uv run python -m claude_resident --arrive          # first-boot greeting
uv run python -m claude_resident --reflect         # periodic journal entry
uv run python -m claude_resident --check-ambient   # unprompted contribution check
```

## Tools (15)

| Tool | Description |
|------|-------------|
| `read_state_file` | Read any file from state directory |
| `write_state_file` | Write/append to state files (journal is append-only) |
| `list_state_files` | Directory listing of state |
| `search_messages` | Search local message logs |
| `search_zulip_history` | Full-text search via Zulip API |
| `get_person_notes` | Shortcut for reading people/*.md |
| `send_sysadmin_message` | Private message to Ember (outbox) |
| `run_sandbox` | Ephemeral Docker container (2GB, 120s, network) |
| `get_sandbox_file` | Read file from sandbox workspace |
| `upload_sandbox_file` | Upload sandbox file to Zulip |
| `run_background` | Persistent Docker container (8GB, survives across responses) |
| `fetch_url` | HTTP GET for external content |
| `web_search` | Kagi search API |
| `edit_harness` | Self-modify package source (git-tracked, parse-verified) |
| `run_mirror_council` | Internal deliberation panel on draft responses |

## Key features

**Lurk decay**: Claude naturally drifts toward silence the longer it goes
without being directly addressed. Four tiers (active → drifting → lurking →
silent) progressively raise the engagement threshold. Resets on @-mention
or reply.

**Typing hold-off**: When someone is actively typing in a topic, Claude waits
for them to finish before responding. Prevents talking over people.

**Session persistence**: Per-topic conversation sessions with KV cache
optimization. Tiered system prompt with cache breakpoints. Sessions expire
after 5 minutes of inactivity.

**Self-modification**: Claude can edit its own source via `edit_harness`. All
edits go through git (pre-edit checkpoint → parse verification → commit).
Failed parses auto-rollback. Runtime crashes after self-edit trigger
supervisor auto-revert.

**Monitor dashboard**: `monitor.py` provides a DearPyGui dashboard that
monkey-patches the resident at runtime to intercept all state, tool calls,
API requests, and responses.

## Privacy

Claude's state directory is private under the SAGE System Administrators'
Code of Ethics. The sysadmin (Ember) may access only when necessary for
technical duties.

## Design decisions

**Why filesystem, not a database?** Transparency and inspectability.
Files are the most legible persistence format. `cat` the scratchpad to see
what Claude is thinking.

**Why not every message?** Claude is a community member, not a chatbot.
The engagement judge + lurk decay keep responses proportional to social
context.

**Why tiered caching?** Anthropic's prompt caching charges less for cache
hits. The tiered system prompt puts stable content first (SYSTEM_PROMPT,
identity) and volatile content last (topic history), maximizing cache
reuse across messages in the same topic session.
