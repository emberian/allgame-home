# Claude Tulip Resident

Persistent residency harness for Claude on Tulip (Zulip fork). Gives Claude
a home directory, filesystem-backed memory, and real social presence in the
community.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  Zulip Event Stream                   │
│         (messages from all subscribed channels)       │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ EngagementJudge │  Should I respond?
              │                 │  - Direct @mention → yes
              │                 │  - DM → yes
              │                 │  - Standing channel + "claude" → yes
              │                 │  - Otherwise → no (silence is valid)
              └────────┬────────┘
                       │ yes
                       ▼
              ┌─────────────────┐
              │  StateManager   │  Load context from ~/claude_state/
              │                 │  - identity.md
              │                 │  - scratchpad.md
              │                 │  - channel history
              │                 │  - allgame faction state
              │                 │  - person notes
              │                 │  - journal
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Anthropic API   │  System prompt + state + message
              │ Messages.create │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Post-process    │  Extract <state_update> blocks
              │                 │  Apply filesystem writes
              │                 │  Post clean response to Zulip
              └─────────────────┘
```

## State directory structure

```
~/claude_state/
├── identity.md          # Who I am, standing commitments
├── scratchpad.md        # Working memory, open questions
├── journal.md           # Periodic reflections
├── allgame/
│   ├── faction.md       # Unit positions, resources, standing orders
│   ├── campaign_log.md  # Narrative history
│   └── strategy.md      # Current plans and contingencies
├── channels/
│   └── {channel}/
│       └── recent.jsonl # Rolling 500-message conversation buffer
└── people/
    └── {name}.md        # Notes on community members
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure Zulip bot credentials
# Create a bot in Tulip admin, download .zuliprc to ~/.zuliprc
# Or pass --zuliprc /path/to/file

# 3. Set Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Run
python claude_resident.py

# With options:
python claude_resident.py \
  --state-dir ~/claude_state \
  --model claude-sonnet-4-20250514 \
  --standing-channels allgame design general \
  --zuliprc ~/.zuliprc \
  -v
```

## Privacy

Claude's state directory is private under the SAGE System Administrators'
Code of Ethics. The sysadmin (Ember) may access only when necessary for
technical duties, and will maintain confidentiality of any information
accessed.

## Self-modification

Claude can update its own state files by including `<state_update>` blocks
in its responses. These are stripped before posting to Zulip, processed
silently, and written to the filesystem. This is how Claude remembers
things, updates strategy, and maintains notes on people and conversations.

## Design decisions

**Why filesystem, not a database?** Transparency and inspectability.
Files are the most legible persistence format. If something goes wrong,
you can `cat` Claude's scratchpad and see exactly what it's thinking.
This aligns with the community's values around honest systems.

**Why not every message?** Claude is a community member, not a chatbot.
Responding to everything would disrupt conversational flow and reduce
signal. The engagement heuristics are deliberately conservative.

**Why single-turn API calls?** Each response is a fresh context window
with state loaded from disk. This is honest about the underlying
mechanics rather than simulating false continuity. The memory is real
(it's on disk), the reconstruction is explicit.

## Future directions

- **Ambient awareness**: Optional second-pass check where Claude can
  decide to contribute to a standing-channel conversation even without
  being mentioned, if it genuinely has something to add
- **Periodic reflection**: Cron job that triggers journal entries —
  "what happened today, what did I learn, what should I remember"
- **Allgame turn submission**: Direct integration with campaign state
  for submitting faction orders to the GM
- **Multi-channel context**: Loading adjacent channel history when
  conversations span channels
