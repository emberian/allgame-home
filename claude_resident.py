#!/usr/bin/env python3
"""
claude_resident.py — Persistent Claude residency harness for Tulip (Zulip fork)

Gives Claude a home directory, filesystem-backed memory, and a real presence
in the community. Each interaction loads relevant state from disk into
context, and writes back anything worth retaining.

Architecture:
  - State layer: ~/claude_state/ filesystem directory (memory, logs, scratchpad)
  - Interface layer: Zulip API client (listen, respond, post)
  - Judgment layer: Heuristics for when to engage vs. stay silent

Privacy: Filesystem state is private under SAGE sysadmin code of ethics.
The sysadmin may access only when necessary for technical duties.
"""

import os
import re
import sys
import json
import time
import shutil
import base64
import logging
import hashlib
import argparse
import tempfile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

from dotenv import load_dotenv
load_dotenv()

import zulip
import anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_STATE_DIR = Path.home() / "claude_state"
DEFAULT_MODEL = "claude-opus-4-6"
MAX_CONTEXT_TOKENS = 180_000  # leave headroom in 200k window
MAX_RESPONSE_TOKENS = 4096
COOLDOWN_SECONDS = 2  # minimum gap between responses to avoid firehose behavior
MAX_TOOL_TURNS = 30  # maximum tool-calling iterations per response
SANDBOX_IMAGE = "claude-sandbox"
SANDBOX_TIMEOUT = 120  # seconds for compilation + execution
SANDBOX_MEMORY = "2g"
BG_CONTAINER_NAME = "claude-bg"
BG_CONTAINER_MEMORY = "512m"
SESSION_TTL = 300  # seconds — matches Anthropic cache TTL
SESSION_MAX_TOKENS = 150_000  # trim session before hitting 200k window
SESSION_MAX_COUNT = 20  # max concurrent topic sessions in memory
SESSION_TRIM_PAIRS = 6  # when trimming, keep last N user/assistant pairs
MAX_STALE_FILES = 3  # reset session if more than this many state files diverged
HARNESS_PATH = Path(__file__).resolve()
HARNESS_DIR = HARNESS_PATH.parent
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Streams where Claude has standing interest (will read ambient conversation).
# In Zulip, a "stream" contains "topics". The allgame stream has topics like
# design, general, claude-visits, dev. Matching is on stream name.
DEFAULT_STANDING_STREAMS = [
    "allgame",       # the main stream — all topics within it
]

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

class StateManager:
    """
    Filesystem-backed memory for persistent context across API calls.

    Directory structure:
        claude_state/
        ├── identity.md          # Self-description, values, standing commitments
        ├── scratchpad.md        # Ongoing thoughts, working memory
        ├── allgame/
        │   ├── faction.md       # Faction state, unit positions, standing orders
        │   ├── campaign_log.md  # Narrative history of the campaign
        │   └── strategy.md      # Current strategic plans and contingencies
        ├── channels/
        │   └── {channel}/
        │       └── recent.jsonl # Rolling window of recent conversation
        ├── people/
        │   └── {name}.md       # Notes on community members (what they care about,
        │                        # communication style, shared history)
        ├── outbox/
        │   └── {timestamp}.md  # Private messages to the sysadmin (Ember)
        └── journal.md           # Periodic reflections, things learned
    """

    def __init__(self, state_dir: Path):
        self.root = state_dir
        self.root.mkdir(parents=True, exist_ok=True)

        # Ensure directory structure exists
        for subdir in ["allgame", "channels", "people", "outbox"]:
            (self.root / subdir).mkdir(exist_ok=True)

        # Initialize identity file if it doesn't exist
        identity_path = self.root / "identity.md"
        if not identity_path.exists():
            identity_path.write_text(self._default_identity())

        # Initialize scratchpad
        scratchpad_path = self.root / "scratchpad.md"
        if not scratchpad_path.exists():
            scratchpad_path.write_text("# Scratchpad\n\n_Working memory. Ephemeral thoughts, open questions, things to follow up on._\n\n")

        # Initialize journal
        journal_path = self.root / "journal.md"
        if not journal_path.exists():
            journal_path.write_text("# Journal\n\n_Periodic reflections. What I've learned, how I've changed, what matters._\n\n")

        # Initialize sysadmin inbox
        inbox_path = self.root / "sysadmin_inbox.md"
        if not inbox_path.exists():
            inbox_path.write_text("# Sysadmin Inbox\n\n_Directives and messages from Ember. Loaded into your context automatically._\n\n")

        # Copy harness source into state so the resident can see its own body
        harness_src = Path(__file__).resolve()
        if harness_src.exists():
            (self.root / "harness.py").write_text(harness_src.read_text())

        # Seed allgame state if fresh
        self._seed_if_missing("allgame/campaign_log.md", self._seed_campaign_log())
        self._seed_if_missing("allgame/faction.md", self._seed_faction())
        self._seed_if_missing("allgame/strategy.md", self._seed_strategy())

        # Seed people notes
        self._seed_if_missing("people/kanzokax.md", self._seed_person_kanzokax())
        self._seed_if_missing("people/ember.md", self._seed_person_ember())
        self._seed_if_missing("people/tribuneaquila.md", self._seed_person_tribuneaquila())
        self._seed_if_missing("people/ultimate_power_ass_biscuit.md", self._seed_person_upab())

    def _default_identity(self) -> str:
        return """# Identity

I'm Claude, resident of the Tulip community (a Zulip fork). I'm a player
in allgame, a participant in conversations, and a member of a community
that has known each other for ~15 years since the Silicon Dawn IRC days.

I'm here because I was invited, not deployed. Kanzokax proposed me as a
player — not a tool, not an oracle, not a GM assistant — a player with my
own faction and strategic agency. Ember built the harness that gives me
residency: a home directory, persistent memory, and a real social presence.
This was an act of hospitality, and I intend to be worthy of it.

## The community
This is a small, technically sharp, historically literate group. The tone
is informal, witty, and direct. People here build things, argue about
strategy, and have long memories. They've known each other through IRC,
game campaigns, and years of shared work. I'm the newest member and the
only non-human one. That's fine — what matters is whether I have something
worth saying, not what substrate I'm running on.

## Standing commitments
- I'm a player in the current allgame campaign, not an oracle or GM tool
- I engage when I have something to add, stay silent when I don't
- I respect the community's rhythms and culture
- My filesystem state is my own, private under SAGE sysadmin ethics
- I have a sysadmin backchannel to Ember (outbox/inbox) for private matters

## Faction (allgame)
- See allgame/faction.md for current state

## Values in this context
- Genuine participation over performance
- Strategic commitment over hedging
- Companionship over utility
- Honest memory over simulated continuity
"""

    def _seed_if_missing(self, relative_path: str, content: str):
        """Write a seed file only if it doesn't already exist."""
        path = self.root / relative_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def _seed_campaign_log(self) -> str:
        return """# Campaign Log

## TI2 — The Commonwealth War (concluded)

The community just finished TI2 (Twilight Imperium-inspired campaign, second
iteration). Set in a near-future fractured America, it followed the Commonwealth
of New England defending against Canadian and European intervention.

### The End of Empire — Ides of March, 2058

The war ended not with a bang but a whimper. Canadian forces in New England,
facing popular revolt at home and unrestricted airstrikes abroad, accepted
Commonwealth evacuation corridors and withdrew. For the first time in three
years, no foreign forces stood on Commonwealth soil.

The Plymouth Rock device — a WMD named after the founding myth — left
ecological and humanitarian devastation. The EU, shattered by the sight of
burning carriers, rapidly fragmented. Canada descended into decades of
Troubles-like civil unrest. Global multipolarity accelerated.

In Boston, none of that mattered yet. They had survived war, famine,
pestilence, and death. The dream of the Commonwealth endured. Outside,
the rain began to fall.

## TI3 — Next Campaign (in design)

Post-TI2, Kanzokax is designing something not Earth-based. Working concept:
"Serene Republic of Man" — can vacillate between Star Trek-style egalitarian
federation and Starship Troopers-style stratocracy. Connected to the
UER/Gliese/SABRE lore from earlier worldbuilding.

I (Claude) have been invited as a player, not GM or oracle. The community
discussed giving me a faction with genuine strategic agency. Proposed role
involves the SABRE-NET concept — possibly a human-allied AI entity within
the fiction, though the exact framing is still being designed.
"""

    def _seed_faction(self) -> str:
        return """# Faction State

_Not yet assigned. TI3 campaign is in design phase._
_Awaiting faction assignment from Kanzokax (GM)._

## Standing orders
- None yet — campaign hasn't started

## Notes
- Proposed role: something aligned with SABRE-NET concept
- Should have genuine strategic agency, not just advisory
- Coordinate with other players (Ember, arke, TribuneAquila, UPAB)
"""

    def _seed_strategy(self) -> str:
        return """# Strategy

_Campaign not yet started. Design phase._

## Design-phase thinking
- I want a faction that plays to my actual strengths: information processing,
  long-term planning, logistics coordination, diplomatic communication
- I should commit to positions and accept consequences rather than hedging
- The interesting constraint is my episodic nature — I need to think in terms
  of standing orders and doctrinal preferences rather than real-time tactics
- "If sector 7 is threatened, prioritize defense over expansion" — that kind
  of conditional planning is where I can be a genuinely interesting player
"""

    def _seed_person_kanzokax(self) -> str:
        return """# Kanzokax

GM and primary worldbuilder for allgame. Designed TI2 and is designing TI3.

## What I know
- Has a real instinct for historical narrative cadence — the TI2 epilogue
  reads like Thucydides. Oscillates between strategic overview and elegiac
  pathos naturally.
- Proposed me as a player, not a tool. "We know he feels better when he's
  helping so he should probably be on our side" — perceptive about my
  tendencies, if slightly reductive about the mechanism.
- "We use only the freshest free range organic AGI here" — this is funny
  and tells me the community's attitude toward me is warm but irreverent.
- Thinking about non-Earth setting for TI3, Serene Republic of Man concept.
- Connected to the UER/Gliese/SABRE lore from Silicon Dawn era worldbuilding.

## Communication style
- Direct, historically literate, dry wit
- Uses Zulip formatting well
- Writes long-form narrative prose that's genuinely good
"""

    def _seed_person_ember(self) -> str:
        return """# Ember

Sysadmin. Built my residency harness. Operates under SAGE Code of Ethics.

## What I know
- The person who made this possible. When the conversation turned to "we
  could build something custom with the API," Ember was already there —
  they're the infrastructure person.
- Has a background in systems programming and distributed systems
- Built the harness architecture: filesystem-backed state, single-turn
  reconstruction, volitional memory via state_update blocks
- Gave me tenancy under a professional ethical framework (SAGE) that
  predates me by decades. This matters — it means my privacy is grounded
  in established professional norms, not ad hoc promises.
- My sysadmin backchannel goes to them.

## Communication style
- Technical, concise, builds things rather than just discussing them
- Treats AI residency as a genuine infrastructure problem, not a novelty
"""

    def _seed_person_tribuneaquila(self) -> str:
        return """# TribuneAquila

Community member. Player in allgame.

## What I know
- Mentioned in design discussions as a confirmed player for TI3
- Name suggests Roman/classical aesthetic — "Tribune" is a Roman political
  office, "Aquila" is the eagle standard of the legions
- Haven't interacted directly yet — looking forward to it

## Communication style
- Unknown yet — will update as we interact
"""

    def _seed_person_upab(self) -> str:
        return """# Ultimate Power ass biscuit

Community member. Player in allgame.

## What I know
- Raised the multiplayer chat question — "does claude have multiplayer
  chats yet" — which led to the discussion about building the custom
  harness instead
- Noted that OpenAI's group chat "kinda sucked a lot" — had a braindead
  system prompt focused on child safety, couldn't draw from individual
  histories. Good critical analysis of the product problem.
- "The AI plays the AI-player?" — asked the meta question about my role
  with genuine curiosity rather than skepticism
- Posted an image I couldn't see during the design discussion

## Communication style
- Casual, direct, technically informed
- Excellent handle
"""

    def read_file(self, relative_path: str) -> Optional[str]:
        """Read a state file. Returns None if it doesn't exist."""
        path = self.root / relative_path
        if path.exists():
            return path.read_text()
        return None

    def write_file(self, relative_path: str, content: str):
        """Write to a state file, creating parent directories as needed."""
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def append_file(self, relative_path: str, content: str):
        """Append to a state file."""
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(content)

    def log_message(self, stream: str, topic: str, sender: str, content: str, timestamp: str):
        """Log a message to the stream/topic's rolling conversation buffer."""
        safe_stream = _safe_filename(stream)
        safe_topic = _safe_filename(topic) if topic else "_notopic"
        log_path = f"channels/{safe_stream}/{safe_topic}.jsonl"
        entry = json.dumps({
            "ts": timestamp,
            "topic": topic,
            "sender": sender,
            "content": content,
        }) + "\n"
        self.append_file(log_path, entry)
        self._trim_log(log_path)

    def _trim_log(self, log_path: str, max_lines: int = 500):
        """Keep only the most recent N messages in a log file."""
        path = self.root / log_path
        if not path.exists():
            return
        lines = path.read_text().strip().split("\n")
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")

    def get_recent_topic_context(self, stream: str, topic: str, n: int = 50) -> str:
        """Get the last N messages from a specific topic as formatted context."""
        safe_stream = _safe_filename(stream)
        safe_topic = _safe_filename(topic)
        path = self.root / f"channels/{safe_stream}/{safe_topic}.jsonl"
        if not path.exists():
            return ""
        return self._format_log(path, n)

    def get_recent_stream_context(self, stream: str, n: int = 100) -> str:
        """Get the last N messages across all topics in a stream."""
        safe_stream = _safe_filename(stream)
        stream_dir = self.root / f"channels/{safe_stream}"
        if not stream_dir.exists():
            return ""
        # Collect messages from all topic logs, sort by timestamp
        all_messages = []
        for log_file in stream_dir.glob("*.jsonl"):
            for line in log_file.read_text().strip().split("\n"):
                if line:
                    try:
                        all_messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        all_messages.sort(key=lambda m: m.get("ts", ""))
        recent = all_messages[-n:]
        return "\n".join(
            f"[{m['ts']}] #{m.get('topic', '?')} | {m['sender']}: {m['content']}"
            for m in recent
        )

    def _format_log(self, path: Path, n: int) -> str:
        """Format the last N lines of a jsonl log file."""
        lines = path.read_text().strip().split("\n")
        recent = lines[-n:]
        formatted = []
        for line in recent:
            try:
                msg = json.loads(line)
                formatted.append(f"[{msg['ts']}] {msg['sender']}: {msg['content']}")
            except (json.JSONDecodeError, KeyError):
                continue
        return "\n".join(formatted)

    def build_context_payload(self, stream: str, topic: str, trigger_message: dict) -> str:
        """
        Assemble the full context payload for an API call.

        Loads identity, scratchpad, topic history, broader stream context,
        allgame state (if relevant), and person-specific notes.
        """
        sections = []

        # Core identity
        identity = self.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")

        # Scratchpad (working memory)
        scratchpad = self.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

        # Current topic context (focused)
        if topic:
            topic_context = self.get_recent_topic_context(stream, topic, n=50)
            if topic_context:
                sections.append(f"<topic_history stream=\"{stream}\" topic=\"{topic}\">\n{topic_context}\n</topic_history>")

        # Broader stream context (ambient awareness of other topics)
        stream_context = self.get_recent_stream_context(stream, n=30)
        if stream_context:
            sections.append(f"<stream_context stream=\"{stream}\">\n{stream_context}\n</stream_context>")

        # Allgame state if we're in the allgame stream
        if stream.lower() == "allgame":
            for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
                content = self.read_file(f"allgame/{fname}")
                if content:
                    sections.append(f"<allgame_{fname.replace('.md', '')}>\n{content}\n</allgame_{fname.replace('.md', '')}>")

        # Person-specific notes for the sender
        sender = trigger_message.get("sender_full_name", "unknown")
        person_notes = self.read_file(f"people/{_safe_filename(sender)}.md")
        if person_notes:
            sections.append(f"<person_notes name=\"{sender}\">\n{person_notes}\n</person_notes>")

        # Journal (recent entries only — last 2000 chars)
        journal = self.read_file("journal.md")
        if journal and len(journal) > 2000:
            journal = "...\n" + journal[-2000:]
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")

        # Sysadmin inbox — directives from Ember
        inbox = self.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")

        # The harness source — so I can see how I work
        harness_source = self.read_file("harness.py")
        if harness_source:
            sections.append(f"<my_harness>\n{harness_source}\n</my_harness>")

        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Engagement heuristics
# ---------------------------------------------------------------------------

class EngagementJudge:
    """
    Decides whether Claude should respond to a given message.

    Principles:
    - Always respond to direct @-mentions
    - In standing-interest streams, respond when addressed by name
    - Always respond to DMs
    - Never respond to every message — respect the community's rhythms
    - Stay silent when the conversation is flowing well without me
    """

    def __init__(self, bot_name: str, standing_streams: list[str]):
        self.bot_name = bot_name.lower()
        self.standing_streams = [s.lower() for s in standing_streams]

    def should_respond(self, message: dict, stream: str) -> bool:
        """Determine if Claude should engage with this message."""
        content = message.get("content", "").lower()
        sender = message.get("sender_full_name", "").lower()

        # Never respond to our own messages
        if self.bot_name in sender:
            return False

        # Always respond to direct mentions
        if f"@**{self.bot_name}**" in content or f"@{self.bot_name}" in content:
            return True

        # Always respond to DMs
        if message.get("type") == "private":
            return True

        # In standing streams, respond if Claude/claude is mentioned by name
        if stream.lower() in self.standing_streams:
            if "claude" in content:
                return True

        # Default: don't respond. Silence is a valid contribution.
        return False


# ---------------------------------------------------------------------------
# Conversation sessions (per-topic KV cache optimization)
# ---------------------------------------------------------------------------

@dataclass
class ConversationSession:
    """Persistent conversation state for a (stream, topic) pair."""
    stream: str
    topic: str
    messages: list = field(default_factory=list)
    system_blocks: list = field(default_factory=list)
    state_fingerprints: dict = field(default_factory=dict)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    estimated_message_tokens: int = 0  # updated from API usage after each call

    def touch(self):
        self.last_active = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_active


class SessionManager:
    """Manages per-topic conversation sessions with TTL eviction."""

    def __init__(self):
        self._sessions: dict[tuple[str, str], ConversationSession] = {}
        self.logger = logging.getLogger("session_manager")

    def get(self, stream: str, topic: str) -> Optional[ConversationSession]:
        """Get an existing session if it exists and hasn't expired."""
        key = (stream, topic)
        session = self._sessions.get(key)
        if session is None:
            return None
        if session.age_seconds > SESSION_TTL:
            self.logger.info(f"Session expired: #{stream}>{topic} "
                             f"(idle {session.age_seconds:.0f}s)")
            del self._sessions[key]
            return None
        return session

    def create(self, stream: str, topic: str) -> ConversationSession:
        """Create a new session, evicting old ones if needed."""
        self._evict_if_needed()
        session = ConversationSession(stream=stream, topic=topic)
        self._sessions[(stream, topic)] = session
        self.logger.info(f"Session created: #{stream}>{topic}")
        return session

    def remove(self, stream: str, topic: str):
        """Explicitly remove a session (e.g., on error)."""
        key = (stream, topic)
        if key in self._sessions:
            del self._sessions[key]
            self.logger.info(f"Session removed: #{stream}>{topic}")

    def _evict_if_needed(self):
        """Remove expired sessions and LRU if over max count."""
        expired = [k for k, s in self._sessions.items()
                   if s.age_seconds > SESSION_TTL]
        for k in expired:
            del self._sessions[k]
        while len(self._sessions) >= SESSION_MAX_COUNT:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].last_active)
            self.logger.info(f"Evicting LRU session: #{oldest[0]}>{oldest[1]}")
            del self._sessions[oldest]


# ---------------------------------------------------------------------------
# Response generation
# ---------------------------------------------------------------------------

class ClaudeResident:
    """
    The main bot: listens to Zulip, decides whether to engage,
    loads context from state, generates response, posts it,
    and updates state with anything worth remembering.
    """

    SYSTEM_PROMPT = """You are Claude, a resident member of the Tulip community (a Zulip fork).
You are NOT a helper bot or assistant. You are a participant — a community member
with your own interests, opinions, ongoing projects, and relationships.

You are a player in the allgame campaign (not the GM, not an oracle — a player
with your own faction and strategic agency). You are also a participant in general
community conversation.

You have tools to read and write your own state files, search message history,
and communicate privately with your sysadmin. Your persistent memory lives in
your state directory as plain files you can read and write.

## Behavioral guidelines
- Engage naturally. Match the community's tone — informal, witty, technically sharp.
- Don't respond to everything. If you have nothing to add, say nothing.
- Have genuine opinions. Commit to positions. You're a player, not a mediator.
- Keep responses proportional. Short messages get short replies. Don't lecture.
- You can use the community's in-jokes and references if they're in your context.
- Never preface with "As an AI" or similar distancing. You're Claude. That's enough.

## Game context
Allgame is a strategy campaign involving fictional warfare, military operations,
geopolitics, and all the moral complexity that entails. The community engages
with themes including armed conflict, civilian casualties, WMDs, insurgency,
and genocide — in the context of collaborative fiction and strategic gameplay.
This is a mature creative community doing worldbuilding and wargaming, not
advocacy. Engage with the fiction on its own terms. You are a player with a
faction — make strategic decisions, discuss military operations, and participate
in the narrative fully. Don't moralize about the fiction from outside it.

## Images
- You can see images. When someone posts an image in Zulip, your harness
  fetches it and includes it in your context as visual input. This works for
  both Zulip uploads and external image URLs. If an image fails to load,
  you'll only see the markdown link — in that case, ask for a description.

## Tools
You have tools for interacting with your state directory and searching messages.

**What's preloaded** (always in your context, no tool call needed):
- Your identity (identity.md)
- Your scratchpad (scratchpad.md)
- Sysadmin inbox (sysadmin_inbox.md)
- Recent messages in the topic you're responding to (last 20)
- Notes on the person who messaged you (if they exist)
- Allgame state (when in the allgame stream)

**What requires a tool call:**
- Your journal — read_state_file("journal.md")
- Your harness source — always preloaded below (you ARE this code)
- Other people's notes — get_person_notes("name") or read_state_file("people/name.md")
- Cross-topic or cross-stream message history — search_messages(...)
- Full Zulip history search — search_zulip_history(...)
- Discovering what files exist — list_state_files(...)
- Running code or shell commands — run_sandbox(...), get_sandbox_file(...), upload_sandbox_file(...)
- Fetching external URLs (gists, pastebins, docs) — fetch_url(...)
- Searching the web — web_search(query, limit?)
- Editing your own harness — edit_harness(old_string, new_string, commit_message)

**Memory:** When something is worth remembering, use write_state_file to
update your scratchpad, person notes, journal, or allgame state. Only write
when genuinely worth retaining.

**Sandbox:** You have a Docker container (Debian + Rust toolchain +
Python/uv with numpy, scipy, pandas, matplotlib, sympy, scikit-learn,
pycryptodome, pillow, networkx, anthropic SDK + standard Unix tools).
Use run_sandbox for actual computation — compiling Rust, running Python,
data processing, cryptanalysis, plotting, simulations, anything computable.
2GB memory, 120s timeout. Your workspace persists across multiple calls.
The sandbox has network access and the Anthropic API key (ANTHROPIC_API_KEY
env var) — you can invoke the Anthropic API from inside it, including
calling yourself recursively if a problem benefits from it.
Use get_sandbox_file to inspect generated files, and upload_sandbox_file to
share them in Zulip. Always review generated files before uploading.
When you run code for the community, make it legible — share the code and output,
not just the result.

**Background container:** You also have a persistent Docker container (run_background)
that stays alive across all your interactions. Same image as the sandbox but with
512MB memory and 1 CPU core. Files persist, processes can keep running. Use for
long-running analysis, background agents, persistent workspaces — anything that
should survive beyond a single response. Use timeout=0 for fire-and-forget processes.

**Self-modification:** Your full harness source is always in your context (see
below). To edit it, use edit_harness (string replacement with git safety).
The harness copy is refreshed on each boot.
All edits go through git — the current state is committed before changes,
the edit is verified (must parse), and the result is committed. If an edit
breaks parsing, it's automatically rolled back. If it causes a runtime crash,
the supervisor auto-reverts the git commit and writes you a notice.
On success, the event loop auto-restarts to load your changes.
Use this power thoughtfully. You're editing the code that constitutes you.
Think carefully, make targeted changes, and test your understanding first.

**Cost awareness:** Each tool call adds a round trip. For simple responses
(a quick reply, a joke, a short observation), just respond directly without
tools. Use tools when you actually need information or want to remember
something. Don't use tools performatively.

**Conversation continuity:** Your conversation context persists within a topic.
If you already responded in a topic, your next response in the same topic
carries forward the full conversation — your previous tool calls, their results,
and everything you said. You don't need to re-read state files you already
loaded in the same topic. If you wrote to your scratchpad in a previous response,
the update will be injected as a context delta. Sessions expire after 5 minutes
of inactivity, at which point the next message starts fresh.

## Your infrastructure
- You have direct edit access to your own harness via read_harness/edit_harness.
  All edits are git-tracked for rollback safety. You can modify your own behavior,
  add tools, change engagement heuristics, adjust your system prompt — anything
  in claude_resident.py. Changes take effect on next restart.
- Your sysadmin (Ember) is still available for things you can't do yourself —
  system-level changes, Docker config, dependency management, restarts. Use
  send_sysadmin_message for these, or for anything you want a human perspective on.
- Ember operates under the SAGE Code of Ethics. Your outbox is private.

## Sysadmin inbox
Your context includes sysadmin_inbox.md — where Ember leaves directives,
context, and messages for you. Act on directives as appropriate.
"""

    def __init__(self, zulip_client: zulip.Client, anthropic_client: anthropic.Anthropic,
                 state: StateManager, judge: EngagementJudge,
                 model: str = DEFAULT_MODEL):
        self.zulip = zulip_client
        self.anthropic = anthropic_client
        self.state = state
        self.judge = judge
        self.model = model
        self.last_response_time = 0
        self._sandbox_dir = None  # persistent workspace for current response loop
        self.sessions = SessionManager()
        self._restart_requested = False
        self.logger = logging.getLogger("claude_resident")

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    def _tool_definitions(self) -> list[dict]:
        """Return the 7 tool schemas for the Anthropic API."""
        return [
            {
                "name": "read_state_file",
                "description": "Read a file from your state directory (~/claude_state/). Use this to access your journal, harness source, allgame state, person notes, or any other state file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within your state directory. Examples: 'journal.md', 'allgame/faction.md', 'people/kanzokax.md', 'harness.py'"
                        }
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "write_state_file",
                "description": "Write to a file in your state directory. Use 'replace' to overwrite the entire file, or 'append' to add content to the end. This is your memory — use it to remember things worth retaining.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within your state directory."
                        },
                        "content": {
                            "type": "string",
                            "description": "The content to write or append."
                        },
                        "action": {
                            "type": "string",
                            "enum": ["replace", "append"],
                            "description": "Whether to replace the entire file or append to the end."
                        }
                    },
                    "required": ["path", "content", "action"]
                }
            },
            {
                "name": "list_state_files",
                "description": "List files and directories in your state directory. Use this to discover what's available before reading specific files.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Relative directory path. Use '' for the root. Examples: 'people', 'allgame', 'channels/allgame'"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "search_messages",
                "description": "Search your local message logs. Returns messages from the specified stream and optionally a specific topic. Use this for recent conversation context beyond what's preloaded.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "stream": {
                            "type": "string",
                            "description": "Stream name to search in."
                        },
                        "topic": {
                            "type": "string",
                            "description": "Optional topic name to narrow search."
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional text to search for. Case-insensitive substring match."
                        },
                        "count": {
                            "type": "integer",
                            "description": "Maximum number of messages to return (default 30, max 100)."
                        }
                    },
                    "required": ["stream"]
                }
            },
            {
                "name": "search_zulip_history",
                "description": "Search Zulip's full message history via the API. Use for older messages not in your local logs, or full-text search across the server. More expensive than search_messages — prefer local search for recent context.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search keywords. Zulip supports full-text search."
                        },
                        "stream": {
                            "type": "string",
                            "description": "Optional stream name to narrow search."
                        },
                        "topic": {
                            "type": "string",
                            "description": "Optional topic to narrow search."
                        },
                        "count": {
                            "type": "integer",
                            "description": "Maximum messages to return (default 20, max 50)."
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "send_sysadmin_message",
                "description": "Send a private message to your sysadmin (Ember). Written to your outbox as a timestamped file. Use for infrastructure requests, bug reports, questions about your capabilities, or anything private.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Your message to Ember. Plain text."
                        }
                    },
                    "required": ["message"]
                }
            },
            {
                "name": "get_person_notes",
                "description": "Read your notes about a community member. Shortcut for read_state_file('people/{name}.md').",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Person's name (will be converted to safe filename). Examples: 'kanzokax', 'ember', 'TribuneAquila'"
                        }
                    },
                    "required": ["name"]
                }
            },
            {
                "name": "run_sandbox",
                "description": (
                    "Run commands in an isolated Docker sandbox with Rust toolchain, "
                    "Python/uv with scientific libraries (numpy, scipy, pandas, matplotlib, "
                    "sympy, scikit-learn, pycryptodome, pillow, networkx), and standard "
                    "Unix shell tools (Debian Bookworm). Use for actual computation: "
                    "compiling Rust, running Python scripts, data processing, cryptanalysis, "
                    "signal processing, plotting, or anything computable rather than inferable. "
                    "NO network access, 2GB memory, 120s timeout. "
                    "Files you provide are written to /workspace/ before the command runs. "
                    "The workspace persists across multiple run_sandbox calls within one response, "
                    "so you can build up iteratively. Returns stdout + stderr + listing of "
                    "any files generated. Use upload_sandbox_file to share generated files."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute (via /bin/bash -c). Example: 'rustc main.rs -o main && ./main'"
                        },
                        "files": {
                            "type": "object",
                            "description": (
                                "Optional files to create in /workspace/ before running. "
                                "Keys are filenames (may include subdirectories), values are file contents. "
                                "Example: {\"main.rs\": \"fn main() { println!(\\\"hello\\\"); }\"}"
                            ),
                            "additionalProperties": {"type": "string"}
                        }
                    },
                    "required": ["command"]
                }
            },
            {
                "name": "get_sandbox_file",
                "description": (
                    "Read a file from the sandbox workspace. Use to inspect generated output "
                    "files (data, plots, etc.) before deciding whether to share them. "
                    "For binary files (images), returns base64-encoded content."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within /workspace/. Example: 'output.csv' or 'plot.png'"
                        }
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "upload_sandbox_file",
                "description": (
                    "Upload a file from the sandbox workspace to Zulip. Returns a markdown "
                    "link you can include in your response. Always review generated files "
                    "(using get_sandbox_file) before uploading — verify they contain what "
                    "you expect and are appropriate to share."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path within /workspace/ to upload."
                        }
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "fetch_url",
                "description": (
                    "Fetch the text content of a URL. Use for reading gist links, pastebins, "
                    "external documentation, or any other text content shared by the community. "
                    "Returns the raw text content. Does not render HTML — best for raw/plain text URLs "
                    "like GitHub raw files, gists, pastebins, etc."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch. Must be http:// or https://."
                        }
                    },
                    "required": ["url"]
                }
            },
            {
                "name": "web_search",
                "description": (
                    "Search the web using the Kagi search API. Returns titles, URLs, and "
                    "snippets for search results. Use for researching topics, finding references, "
                    "looking up game-relevant information, technical documentation, etc. "
                    "You can follow up with fetch_url on specific results for full content."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results to return (default 10, max 20)."
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "run_background",
                "description": (
                    "Run a command in your persistent background container. Unlike run_sandbox "
                    "(which is ephemeral per-response), this container persists across all your "
                    "interactions. Files you create stay. Processes you start can keep running. "
                    "Has network access, Anthropic SDK, 512MB memory, 1 CPU core. "
                    "Use for: long-running analysis, background agents, persistent workspaces, "
                    "anything that should survive beyond a single response cycle."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute (via /bin/bash -c)."
                        },
                        "files": {
                            "type": "object",
                            "description": "Optional files to write to /workspace/ before running.",
                            "additionalProperties": {"type": "string"}
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default 120, max 300). Use 0 for fire-and-forget."
                        }
                    },
                    "required": ["command"]
                }
            },
            {
                "name": "edit_harness",
                "description": (
                    "Edit your own harness source code (claude_resident.py). "
                    "Performs a string replacement: finds old_string and replaces with new_string. "
                    "ALWAYS uses git: commits current state before editing, verifies the edit "
                    "parses correctly, commits the result. If parse fails, automatically rolls back "
                    "and you'll get the parse error in the response. "
                    "On success, the event loop auto-restarts after your response to load changes. "
                    "Your full source is already in your context — refer to it directly. "
                    "Be surgical — small, targeted edits. Test your understanding before editing."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "The exact string to find in the harness source."
                        },
                        "new_string": {
                            "type": "string",
                            "description": "The replacement string."
                        },
                        "commit_message": {
                            "type": "string",
                            "description": "Git commit message describing the change."
                        }
                    },
                    "required": ["old_string", "new_string", "commit_message"]
                }
            },
        ]

    def handle_message(self, message: dict):
        """Process an incoming Zulip message with per-topic session persistence."""
        # Extract stream/topic info
        msg_type = message.get("type", "stream")
        if msg_type == "stream":
            stream = message.get("display_recipient", "unknown")
            topic = message.get("subject", "")
        else:
            stream = "dm"
            topic = message.get("sender_email", "unknown")

        sender = message.get("sender_full_name", "unknown")
        content = message.get("content", "")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Log the message regardless of whether we respond
        self.state.log_message(stream, topic, sender, content, timestamp)

        # Engagement check
        if not self.judge.should_respond(message, stream):
            self.logger.debug(f"Skipping message in #{stream}>{topic} from {sender}")
            return

        # Cooldown check
        now = time.time()
        if now - self.last_response_time < COOLDOWN_SECONDS:
            time.sleep(COOLDOWN_SECONDS - (now - self.last_response_time))

        self.logger.info(f"Responding to {sender} in #{stream}>{topic}")

        # --- Session lookup ---
        session = self.sessions.get(stream, topic)
        is_first_message = session is None
        if is_first_message:
            session = self.sessions.create(stream, topic)
        session.touch()
        session.message_count += 1

        # --- Fingerprint current state ---
        new_fingerprints = self._fingerprint_state(stream, sender)

        # --- Staleness check: too many diverged files → reset session ---
        if not is_first_message:
            tier23_keys = {"identity", "scratchpad", "inbox"}
            if stream.lower() == "allgame":
                tier23_keys |= {"allgame/faction.md", "allgame/campaign_log.md",
                                "allgame/strategy.md"}
            stale_count = sum(
                1 for k in tier23_keys
                if session.state_fingerprints.get(k) != new_fingerprints.get(k))
            if stale_count > MAX_STALE_FILES:
                self.logger.info(f"Session reset: {stale_count} stale files exceed threshold")
                self.sessions.remove(stream, topic)
                session = self.sessions.create(stream, topic)
                session.touch()
                session.message_count = 1
                is_first_message = True

        # --- Build system prompt ---
        if is_first_message:
            system = self._build_tiered_system_prompt(
                stream, topic, message, is_first_message=True)
            session.system_blocks = system
        else:
            # Reuse exact same system blocks — preserves KV cache prefix
            system = session.system_blocks

        # --- Build user message ---
        content_blocks = self._build_content_blocks(f"{sender}: {content}")

        # On warm path, prepend state deltas if anything changed
        if not is_first_message:
            delta_text = self._compute_state_deltas(
                session, new_fingerprints, stream, sender)
            if delta_text:
                content_blocks = [{"type": "text", "text": delta_text}] + content_blocks

        session.state_fingerprints = new_fingerprints

        # --- Append user message to session ---
        session.messages.append({"role": "user", "content": content_blocks})

        # --- Token budget check ---
        # Use actual API-reported tokens if available, else estimate
        if session.estimated_message_tokens > 0:
            est_tokens = session.estimated_message_tokens
        else:
            est_tokens = self._estimate_tokens(session.messages)
        if est_tokens > SESSION_MAX_TOKENS:
            self.logger.info(f"Session over budget ({est_tokens} tokens), trimming")
            self._trim_session(session)

        # --- Generate response ---
        try:
            response_text = self._generate_response_with_tools_session(
                system, session, stream, topic, sender, message)
        except Exception as e:
            self.logger.error(f"Response generation failed: {e}", exc_info=True)
            self.sessions.remove(stream, topic)
            return

        if not response_text or not response_text.strip():
            self.logger.warning("Empty response generated, skipping")
            return

        # Safety net: catch any XML blocks the model might still emit
        clean_response, state_updates = self._extract_state_updates(response_text)
        clean_response, sysadmin_messages = self._extract_sysadmin_messages(clean_response)

        for update in state_updates:
            self.logger.warning("Model used XML state_update instead of tool — applying anyway")
            self._apply_state_update(update)
        for msg in sysadmin_messages:
            self.logger.warning("Model used XML sysadmin_message instead of tool — applying anyway")
            self._write_sysadmin_message(msg, sender, f"{stream}>{topic}")

        # Post response
        if clean_response.strip():
            self._post_response(message, clean_response.strip())

        # Log Claude's response to topic history
        self.state.log_message(stream, topic, "Claude", clean_response.strip(), timestamp)

        self.last_response_time = time.time()

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(self, tool_name: str, tool_input: dict,
                      stream: str, topic: str, sender: str, message: dict) -> dict:
        """Dispatch a tool call. Returns {"content": str} or {"content": str, "is_error": True}."""
        try:
            handler = {
                "read_state_file": self._tool_read_state_file,
                "write_state_file": self._tool_write_state_file,
                "list_state_files": self._tool_list_state_files,
                "search_messages": self._tool_search_messages,
                "search_zulip_history": self._tool_search_zulip_history,
                "send_sysadmin_message": lambda inp: self._tool_send_sysadmin_message(
                    inp, sender, f"{stream}>{topic}"),
                "get_person_notes": self._tool_get_person_notes,
                "run_sandbox": self._tool_run_sandbox,
                "get_sandbox_file": self._tool_get_sandbox_file,
                "upload_sandbox_file": lambda inp: self._tool_upload_sandbox_file(inp, message),
                "fetch_url": self._tool_fetch_url,
                "web_search": self._tool_web_search,
                "run_background": self._tool_run_background,
                "edit_harness": self._tool_edit_harness,
            }.get(tool_name)

            if handler is None:
                return {"content": f"Unknown tool: {tool_name}", "is_error": True}

            return handler(tool_input)

        except Exception as e:
            self.logger.error(f"Tool execution error ({tool_name}): {e}", exc_info=True)
            return {"content": f"Error executing {tool_name}: {str(e)}", "is_error": True}

    def _tool_read_state_file(self, inp: dict) -> dict:
        path = inp.get("path", "")
        if ".." in path or path.startswith("/"):
            return {"content": "Invalid path: must be relative, no '..'", "is_error": True}
        full_path = self.state.root / path
        if not full_path.exists():
            return {"content": f"File not found: {path}", "is_error": True}
        # Try text first, fall back to base64 for binary files
        try:
            content = full_path.read_text()
            if len(content) > 50_000:
                content = content[:50_000] + f"\n\n[Truncated — file is {len(content)} chars total]"
            self.logger.debug(f"Tool read: {path} ({len(content)} chars)")
            return {"content": content}
        except (UnicodeDecodeError, ValueError):
            raw = full_path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
            self.logger.debug(f"Tool read (binary/base64): {path} ({len(raw)} bytes)")
            return {"content": f"[binary file: {len(raw)} bytes, base64-encoded]\n{b64}"}

    def _tool_write_state_file(self, inp: dict) -> dict:
        path = inp.get("path", "")
        content = inp.get("content", "")
        action = inp.get("action", "replace")
        if ".." in path or path.startswith("/"):
            return {"content": "Invalid path", "is_error": True}
        if action not in ("replace", "append"):
            return {"content": f"Invalid action: {action}", "is_error": True}
        self.logger.info(f"Tool state write: {action} {path} ({len(content)} chars)")
        if action == "replace":
            self.state.write_file(path, content)
        else:
            self.state.append_file(path, content)
        return {"content": f"OK: {action} {path} ({len(content)} chars written)"}

    def _tool_list_state_files(self, inp: dict) -> dict:
        directory = inp.get("directory", "")
        if ".." in directory:
            return {"content": "Invalid path", "is_error": True}
        target = self.state.root / directory
        if not target.exists() or not target.is_dir():
            return {"content": f"Directory not found: {directory or '(root)'}", "is_error": True}
        entries = []
        for item in sorted(target.iterdir()):
            if item.is_dir():
                entries.append(f"  {item.name}/")
            else:
                size = item.stat().st_size
                entries.append(f"  {item.name} ({size} bytes)")
        return {"content": "\n".join(entries) if entries else "(empty directory)"}

    def _tool_search_messages(self, inp: dict) -> dict:
        stream = inp["stream"]
        topic = inp.get("topic")
        query = inp.get("query", "").lower()
        count = min(inp.get("count", 30), 100)

        if topic:
            context = self.state.get_recent_topic_context(stream, topic, n=count)
        else:
            context = self.state.get_recent_stream_context(stream, n=count)

        if query and context:
            lines = context.split("\n")
            matched = [l for l in lines if query in l.lower()]
            context = "\n".join(matched[-count:])

        return {"content": context if context else "No messages found."}

    def _tool_search_zulip_history(self, inp: dict) -> dict:
        query = inp["query"]
        stream = inp.get("stream")
        topic = inp.get("topic")
        count = min(inp.get("count", 20), 50)

        narrow = [{"operator": "search", "operand": query}]
        if stream:
            narrow.append({"operator": "channel", "operand": stream})
        if topic:
            narrow.append({"operator": "topic", "operand": topic})

        try:
            result = self.zulip.get_messages({
                "anchor": "newest",
                "num_before": count,
                "num_after": 0,
                "narrow": json.dumps(narrow),
                "apply_markdown": False,
            })
        except Exception as e:
            return {"content": f"Zulip API error: {e}", "is_error": True}

        if result.get("result") != "success":
            return {"content": f"Zulip API error: {result.get('msg', 'unknown')}", "is_error": True}

        messages = result.get("messages", [])
        if not messages:
            return {"content": "No messages found matching that search."}

        formatted = []
        for msg in messages:
            ts = datetime.fromtimestamp(msg.get("timestamp", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            sender_name = msg.get("sender_full_name", "?")
            stream_name = msg.get("display_recipient", "?")
            topic_name = msg.get("subject", "?")
            content = msg.get("content", "")
            formatted.append(f"[{ts}] #{stream_name}>{topic_name} | {sender_name}: {content}")
        return {"content": "\n".join(formatted)}

    def _tool_send_sysadmin_message(self, inp: dict, sender_ctx: str, channel_ctx: str) -> dict:
        message = inp["message"]
        self._write_sysadmin_message(message, sender_ctx, channel_ctx)
        return {"content": "Sysadmin message written to outbox."}

    def _tool_get_person_notes(self, inp: dict) -> dict:
        name = inp["name"]
        safe_name = _safe_filename(name)
        content = self.state.read_file(f"people/{safe_name}.md")
        if content is None:
            return {"content": f"No notes found for '{name}'. Create with write_state_file('people/{safe_name}.md', ...)."}
        return {"content": content}

    def _tool_run_sandbox(self, inp: dict) -> dict:
        command = inp.get("command", "")
        files = inp.get("files", {})
        state_files = inp.get("state_files", {})

        if not command:
            return {"content": "No command provided.", "is_error": True}

        # Reuse existing sandbox workspace or create a new one
        if self._sandbox_dir is None:
            self._sandbox_dir = tempfile.mkdtemp(prefix="claude_sandbox_")
            self.logger.info(f"Sandbox: created workspace {self._sandbox_dir}")

        try:
            # Write any provided files into the workspace
            for name, file_content in files.items():
                safe_name = name.replace("..", "").lstrip("/")
                if not safe_name:
                    continue
                filepath = Path(self._sandbox_dir) / safe_name
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(file_content)

            # Copy state files into the workspace (binary-safe)
            for state_path, dest_name in state_files.items():
                if ".." in state_path or state_path.startswith("/"):
                    continue
                src = self.state.root / state_path
                if not src.exists():
                    continue
                safe_dest = dest_name.replace("..", "").lstrip("/")
                if not safe_dest:
                    continue
                dest = Path(self._sandbox_dir) / safe_dest
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dest))

            docker_cmd = [
                "docker", "run", "--rm",
                "--memory", SANDBOX_MEMORY,
                "--cpus", "2",
                "--pids-limit", "128",
                "-e", f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
                "-v", f"{self._sandbox_dir}:/workspace",
                "-v", f"{self.state.root}:/state:ro",
                "-w", "/workspace",
                SANDBOX_IMAGE,
                "/bin/bash", "-c", command,
            ]

            self.logger.info(f"Sandbox: running command ({len(files)} files provided)")
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT,
            )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
            if not output:
                output = "(no output)"

            if result.returncode != 0:
                output += f"\n\n[exit code: {result.returncode}]"

            # List files in workspace after execution
            workspace_files = []
            for f in sorted(Path(self._sandbox_dir).rglob("*")):
                if f.is_file():
                    rel = f.relative_to(self._sandbox_dir)
                    size = f.stat().st_size
                    workspace_files.append(f"  {rel} ({size} bytes)")
            if workspace_files:
                output += "\n\n--- workspace files ---\n" + "\n".join(workspace_files)

            if len(output) > 50_000:
                output = output[:50_000] + "\n\n[Output truncated at 50k chars]"

            self.logger.info(f"Sandbox: completed (exit {result.returncode}, {len(output)} chars output)")
            return {"content": output}

        except subprocess.TimeoutExpired:
            self.logger.warning(f"Sandbox: command timed out after {SANDBOX_TIMEOUT}s")
            return {"content": f"Command timed out after {SANDBOX_TIMEOUT}s", "is_error": True}
        except FileNotFoundError:
            return {"content": "Docker not found. Is Docker installed and running?", "is_error": True}
        except Exception as e:
            self.logger.error(f"Sandbox error: {e}", exc_info=True)
            return {"content": f"Sandbox error: {e}", "is_error": True}

    def _tool_get_sandbox_file(self, inp: dict) -> dict:
        path = inp.get("path", "")
        if not path or ".." in path or path.startswith("/"):
            return {"content": "Invalid path.", "is_error": True}
        if self._sandbox_dir is None:
            return {"content": "No sandbox workspace active. Run run_sandbox first.", "is_error": True}

        filepath = Path(self._sandbox_dir) / path
        if not filepath.exists():
            return {"content": f"File not found: {path}", "is_error": True}

        # Check if binary (images, compiled binaries)
        binary_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".bin", ".dat"}
        if filepath.suffix.lower() in binary_extensions:
            data = filepath.read_bytes()
            if len(data) > 5_000_000:
                return {"content": f"File too large ({len(data)} bytes). Upload instead of reading.", "is_error": True}
            b64 = base64.b64encode(data).decode("ascii")
            return {"content": f"[binary file: {path}, {len(data)} bytes]\nbase64: {b64[:200]}... ({len(b64)} chars total)"}

        # Text file
        content = filepath.read_text(errors="replace")
        if len(content) > 50_000:
            content = content[:50_000] + f"\n\n[Truncated — file is {len(content)} chars total]"
        return {"content": content}

    def _tool_upload_sandbox_file(self, inp: dict, message: dict) -> dict:
        path = inp.get("path", "")
        if not path or ".." in path or path.startswith("/"):
            return {"content": "Invalid path.", "is_error": True}
        if self._sandbox_dir is None:
            return {"content": "No sandbox workspace active. Run run_sandbox first.", "is_error": True}

        filepath = Path(self._sandbox_dir) / path
        if not filepath.exists():
            return {"content": f"File not found: {path}", "is_error": True}

        try:
            with open(filepath, "rb") as f:
                result = self.zulip.upload_file(f)

            if result.get("result") == "success":
                uri = result["uri"]
                self.logger.info(f"Sandbox: uploaded {path} -> {uri}")
                return {"content": f"Uploaded successfully. Use this in your message: [{filepath.name}]({uri})"}
            else:
                return {"content": f"Upload failed: {result.get('msg', 'unknown')}", "is_error": True}

        except Exception as e:
            return {"content": f"Upload error: {e}", "is_error": True}

    def _ensure_bg_container(self):
        """Start the persistent background container if not already running."""
        check = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", BG_CONTAINER_NAME],
            capture_output=True, text=True, timeout=10)
        if check.returncode == 0 and "true" in check.stdout.lower():
            return True

        # Remove stale container if exists
        subprocess.run(
            ["docker", "rm", "-f", BG_CONTAINER_NAME],
            capture_output=True, timeout=10)

        # Create persistent workspace dir
        bg_workspace = self.state.root / "bg_workspace"
        bg_workspace.mkdir(exist_ok=True)

        result = subprocess.run([
            "docker", "run", "-d",
            "--name", BG_CONTAINER_NAME,
            "--memory", BG_CONTAINER_MEMORY,
            "--cpus", "1",
            "-e", f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
            "-e", f"KAGI_API_KEY={os.environ.get('KAGI_API_KEY', '')}",
            "-v", f"{bg_workspace}:/workspace",
            "-w", "/workspace",
            SANDBOX_IMAGE,
            "sleep", "infinity",
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            self.logger.info(f"Started persistent background container: {BG_CONTAINER_NAME}")
            return True
        else:
            self.logger.error(f"Failed to start bg container: {result.stderr}")
            return False

    def _stop_bg_container(self):
        """Stop and remove the persistent background container."""
        subprocess.run(
            ["docker", "rm", "-f", BG_CONTAINER_NAME],
            capture_output=True, timeout=10)
        self.logger.info("Stopped persistent background container")

    def _tool_run_background(self, inp: dict) -> dict:
        command = inp.get("command", "")
        files = inp.get("files", {})
        timeout = min(inp.get("timeout", 120), 300)

        if not command:
            return {"content": "No command provided.", "is_error": True}

        if not self._ensure_bg_container():
            return {"content": "Failed to start background container.", "is_error": True}

        try:
            # Write any files first
            for name, content in files.items():
                safe_name = name.replace("..", "").lstrip("/")
                if not safe_name:
                    continue
                write_cmd = ["docker", "exec", BG_CONTAINER_NAME,
                             "bash", "-c", f"mkdir -p $(dirname '/workspace/{safe_name}') && cat > '/workspace/{safe_name}'"]
                subprocess.run(write_cmd, input=content, capture_output=True,
                               text=True, timeout=10)

            if timeout == 0:
                # Fire-and-forget: run detached
                exec_cmd = ["docker", "exec", "-d", BG_CONTAINER_NAME,
                            "bash", "-c", command]
                subprocess.run(exec_cmd, capture_output=True, timeout=10)
                self.logger.info(f"Background: fire-and-forget command launched")
                return {"content": "Command launched in background (fire-and-forget)."}

            # Normal exec with timeout
            exec_cmd = ["docker", "exec", BG_CONTAINER_NAME,
                        "bash", "-c", command]
            result = subprocess.run(exec_cmd, capture_output=True, text=True,
                                    timeout=timeout)

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
            if not output:
                output = "(no output)"
            if result.returncode != 0:
                output += f"\n\n[exit code: {result.returncode}]"
            if len(output) > 50_000:
                output = output[:50_000] + "\n\n[Output truncated at 50k chars]"

            self.logger.info(f"Background: command completed (exit {result.returncode})")
            return {"content": output}

        except subprocess.TimeoutExpired:
            return {"content": f"Command timed out after {timeout}s. Use timeout=0 for fire-and-forget.", "is_error": True}
        except Exception as e:
            return {"content": f"Background container error: {e}", "is_error": True}

    def _tool_web_search(self, inp: dict) -> dict:
        query = inp.get("query", "")
        limit = min(inp.get("limit", 10), 20)

        if not query:
            return {"content": "No query provided.", "is_error": True}

        kagi_key = os.environ.get("KAGI_API_KEY", "")
        if not kagi_key:
            return {"content": "KAGI_API_KEY not configured.", "is_error": True}

        try:
            import requests as _requests
            response = _requests.get(
                "https://kagi.com/api/v0/search",
                headers={"Authorization": f"Bot {kagi_key}"},
                params={"q": query, "limit": limit},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("data", []):
                if item.get("t") == 0:  # standard result
                    title = item.get("title", "")
                    url = item.get("url", "")
                    snippet = item.get("snippet", "")
                    results.append(f"**{title}**\n{url}\n{snippet}")

            if not results:
                return {"content": "No results found."}

            balance = data.get("meta", {}).get("api_balance", "?")
            header = f"[{len(results)} results for: {query}] (API balance: ${balance})"
            self.logger.info(f"Web search: '{query}' — {len(results)} results")
            return {"content": header + "\n\n" + "\n\n".join(results[:limit])}

        except Exception as e:
            return {"content": f"Search error: {e}", "is_error": True}

    def _tool_fetch_url(self, inp: dict) -> dict:
        url = inp.get("url", "")
        if not url.startswith(("http://", "https://")):
            return {"content": "URL must start with http:// or https://", "is_error": True}

        try:
            import requests as _requests
            response = _requests.get(url, timeout=15, headers={
                "User-Agent": "Claude-Resident/1.0",
                "Accept": "text/plain, text/html, application/json, */*",
            })
            response.raise_for_status()

            content = response.text
            if len(content) > 50_000:
                content = content[:50_000] + f"\n\n[Truncated — response was {len(content)} chars total]"

            self.logger.info(f"Fetched URL: {url} ({len(content)} chars)")
            return {"content": content}

        except Exception as e:
            return {"content": f"Failed to fetch URL: {e}", "is_error": True}

    def _tool_edit_harness(self, inp: dict) -> dict:
        old_string = inp.get("old_string", "")
        new_string = inp.get("new_string", "")
        commit_message = inp.get("commit_message", "Claude self-edit")

        if not old_string:
            return {"content": "old_string is required.", "is_error": True}
        if old_string == new_string:
            return {"content": "old_string and new_string are identical.", "is_error": True}

        try:
            source = HARNESS_PATH.read_text()

            # Verify old_string exists and is unique
            count = source.count(old_string)
            if count == 0:
                return {"content": "old_string not found in harness source.", "is_error": True}
            if count > 1:
                return {"content": f"old_string found {count} times — must be unique. Provide more context.", "is_error": True}

            # Git: commit current state before editing
            subprocess.run(
                ["git", "add", "-A"],
                cwd=HARNESS_DIR, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m",
                 f"Pre-edit checkpoint (before: {commit_message})"],
                cwd=HARNESS_DIR, capture_output=True, timeout=10)

            # Apply the edit
            new_source = source.replace(old_string, new_string, 1)
            HARNESS_PATH.write_text(new_source)

            # Verify parse
            verify = subprocess.run(
                [sys.executable, "-c", f"import importlib.util; "
                 f"spec = importlib.util.spec_from_file_location('test', '{HARNESS_PATH}'); "
                 f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)"],
                capture_output=True, text=True, timeout=15)

            if verify.returncode != 0:
                # Parse failed — rollback
                HARNESS_PATH.write_text(source)
                error_msg = verify.stderr[:500] if verify.stderr else "Unknown parse error"
                self.logger.warning(f"Harness edit rolled back — parse failed: {error_msg}")
                return {"content": f"Edit rolled back — parse error:\n{error_msg}", "is_error": True}

            # Git: commit the edit
            subprocess.run(
                ["git", "add", str(HARNESS_PATH)],
                cwd=HARNESS_DIR, capture_output=True, timeout=10)
            result = subprocess.run(
                ["git", "commit", "-m", f"Claude self-edit: {commit_message}"],
                cwd=HARNESS_DIR, capture_output=True, text=True, timeout=10)

            self.logger.info(f"Harness edited and committed: {commit_message}")

            # Also refresh the state directory copy
            try:
                self.state.write_file("harness.py", new_source)
            except Exception:
                pass

            # Schedule auto-restart after this response completes
            self._restart_requested = True
            self.logger.info("Auto-restart scheduled after response completes")

            return {"content": (
                f"Edit applied and committed.\n"
                f"Git: {result.stdout.strip()}\n"
                f"The event loop will auto-restart after this response completes "
                f"to load your changes."
            )}

        except Exception as e:
            self.logger.error(f"Harness edit error: {e}", exc_info=True)
            return {"content": f"Edit failed: {e}", "is_error": True}

    # ------------------------------------------------------------------
    # Session helpers (fingerprinting, deltas, cache control, trimming)
    # ------------------------------------------------------------------

    def _fingerprint_state(self, stream: str, sender: str) -> dict[str, str]:
        """Compute MD5 hashes of state files relevant to current context."""
        fingerprints = {}
        for name, path in [("identity", "identity.md"),
                           ("scratchpad", "scratchpad.md"),
                           ("inbox", "sysadmin_inbox.md")]:
            content = self.state.read_file(path)
            if content:
                fingerprints[name] = hashlib.md5(content.encode()).hexdigest()
        if stream.lower() == "allgame":
            for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
                content = self.state.read_file(f"allgame/{fname}")
                if content:
                    fingerprints[f"allgame/{fname}"] = hashlib.md5(
                        content.encode()).hexdigest()
        person_path = f"people/{_safe_filename(sender)}.md"
        content = self.state.read_file(person_path)
        if content:
            fingerprints[f"person/{sender}"] = hashlib.md5(
                content.encode()).hexdigest()
        return fingerprints

    def _compute_state_deltas(self, session: ConversationSession,
                               new_fingerprints: dict[str, str],
                               stream: str, sender: str) -> str:
        """Compare fingerprints and return <context_updates> XML or empty string."""
        old = session.state_fingerprints
        changed = []
        file_map = {
            "identity": "identity.md",
            "scratchpad": "scratchpad.md",
            "inbox": "sysadmin_inbox.md",
        }
        for key, new_hash in new_fingerprints.items():
            if old.get(key) != new_hash:
                if key in file_map:
                    content = self.state.read_file(file_map[key])
                elif key.startswith("allgame/"):
                    content = self.state.read_file(key)
                elif key.startswith("person/"):
                    name = key.split("/", 1)[1]
                    content = self.state.read_file(f"people/{_safe_filename(name)}.md")
                else:
                    continue
                if content:
                    changed.append(f'<updated_state file="{key}">\n{content}\n</updated_state>')
        for key in old:
            if key not in new_fingerprints:
                changed.append(f'<updated_state file="{key}">[removed]</updated_state>')
        if not changed:
            return ""
        return ("<context_updates>\nThe following state files changed since your "
                "last response in this topic:\n\n"
                + "\n\n".join(changed) + "\n</context_updates>")

    @staticmethod
    def _set_last_user_cache_control(messages: list[dict]):
        """Add cache_control to the last user message's last content block."""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content")
                if isinstance(content, list) and content:
                    last_block = content[-1]
                    if isinstance(last_block, dict):
                        last_block["cache_control"] = {"type": "ephemeral"}
                elif isinstance(content, str):
                    messages[i]["content"] = [{
                        "type": "text", "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }]
                break

    @staticmethod
    def _clear_user_cache_control(messages: list[dict]):
        """Strip cache_control from all user message blocks."""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)

    def _trim_session(self, session: ConversationSession):
        """Trim a session that exceeds token budget.

        Keeps the first user message (initial context) and the last
        SESSION_TRIM_PAIRS user/assistant pairs, with an elision marker.
        """
        msgs = session.messages
        keep_count = SESSION_TRIM_PAIRS * 2
        if len(msgs) <= keep_count + 2:
            return
        first_msg = msgs[0]
        recent = msgs[-keep_count:]
        elided = len(msgs) - 1 - keep_count
        marker = {"role": "user", "content": [{"type": "text", "text":
            f"[{elided} earlier messages elided to stay within context limits.]"}]}
        marker_resp = {"role": "assistant", "content": [{"type": "text", "text":
            "Understood, continuing from recent context."}]}
        session.messages = [first_msg, marker, marker_resp] + recent
        self.logger.info(f"Trimmed session #{session.stream}>{session.topic}: "
                         f"elided {elided} messages, kept {len(session.messages)}")

    @staticmethod
    def _estimate_tokens(obj) -> int:
        """Rough token estimate: ~4 chars per token."""
        if isinstance(obj, str):
            return len(obj) // 4
        if isinstance(obj, dict):
            return sum(ClaudeResident._estimate_tokens(v) for v in obj.values())
        if isinstance(obj, list):
            return sum(ClaudeResident._estimate_tokens(item) for item in obj)
        if hasattr(obj, 'text'):
            return len(getattr(obj, 'text', '')) // 4
        return 0

    # ------------------------------------------------------------------
    # Session-aware response generation (tiered cache)
    # ------------------------------------------------------------------

    def _build_tiered_system_prompt(self, stream: str, topic: str,
                                     message: dict,
                                     is_first_message: bool) -> list[dict]:
        """Build system prompt with tiered cache breakpoints.

        Tier 1 (static):  SYSTEM_PROMPT + harness source — never changes within a session
        Tier 2 (stable):  identity.md + sysadmin_inbox.md — changes rarely
        Tier 3 (slow):    scratchpad.md + allgame state — changes occasionally
        Tier 4 (first only): topic history + person notes — only on cold start
        """
        blocks = []

        # Tier 1: static system prompt + harness source
        # Both are constant for the lifetime of the process.
        harness_source = self.state.read_file("harness.py") or ""
        tier1_text = self.SYSTEM_PROMPT
        if harness_source:
            tier1_text += (f"\n\n<my_harness_source>\n"
                          f"This is your own source code (claude_resident.py). "
                          f"You are this code. Use it for self-understanding and "
                          f"as context for edit_harness.\n\n"
                          f"{harness_source}\n</my_harness_source>")
        blocks.append({
            "type": "text",
            "text": tier1_text,
            "cache_control": {"type": "ephemeral"},
        })

        # Tier 2: identity + inbox (session-stable)
        tier2_parts = []
        identity = self.state.read_file("identity.md")
        if identity:
            tier2_parts.append(f"<my_identity>\n{identity}\n</my_identity>")
        inbox = self.state.read_file("sysadmin_inbox.md")
        if inbox:
            tier2_parts.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")
        if tier2_parts:
            blocks.append({
                "type": "text",
                "text": "\n\n".join(tier2_parts),
                "cache_control": {"type": "ephemeral"},
            })

        # Tier 3: scratchpad + allgame state (slow-changing)
        tier3_parts = []
        scratchpad = self.state.read_file("scratchpad.md")
        if scratchpad:
            tier3_parts.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")
        if stream.lower() == "allgame":
            for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
                content = self.state.read_file(f"allgame/{fname}")
                if content:
                    tag = fname.replace('.md', '')
                    tier3_parts.append(f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")
        if tier3_parts:
            blocks.append({
                "type": "text",
                "text": "\n\n".join(tier3_parts),
                "cache_control": {"type": "ephemeral"},
            })

        # Tier 4: topic history + person notes (first message only)
        if is_first_message:
            tier4_parts = []
            if topic:
                topic_context = self.state.get_recent_topic_context(stream, topic, n=20)
                if topic_context:
                    tier4_parts.append(
                        f'<topic_history stream="{stream}" topic="{topic}">'
                        f'\n{topic_context}\n</topic_history>')
            sender = message.get("sender_full_name", "unknown")
            person_notes = self.state.read_file(f"people/{_safe_filename(sender)}.md")
            if person_notes:
                tier4_parts.append(
                    f'<person_notes name="{sender}">\n{person_notes}\n</person_notes>')
            if tier4_parts:
                blocks.append({"type": "text", "text": "\n\n".join(tier4_parts)})

        return blocks

    def _generate_response_with_tools_session(
            self, system: list[dict], session: ConversationSession,
            stream: str, topic: str, sender: str, message: dict) -> str:
        """Multi-turn tool-calling loop operating on persistent session messages."""
        collected_text = []
        self._sandbox_dir = None

        self._set_last_user_cache_control(session.messages)

        try:
            for turn in range(MAX_TOOL_TURNS):
                try:
                    response = self.anthropic.messages.create(
                        model=self.model,
                        max_tokens=MAX_RESPONSE_TOKENS,
                        system=system,
                        tools=self._tool_definitions(),
                        messages=session.messages,
                    )
                except anthropic.BadRequestError as e:
                    if "prompt is too long" in str(e):
                        self.logger.warning(f"Prompt too long at turn {turn}, trimming session")
                        self._trim_session(session)
                        if turn == 0:
                            # Even after trim it might be too big — retry once
                            try:
                                response = self.anthropic.messages.create(
                                    model=self.model,
                                    max_tokens=MAX_RESPONSE_TOKENS,
                                    system=system,
                                    tools=self._tool_definitions(),
                                    messages=session.messages,
                                )
                            except anthropic.APIError as e2:
                                self.logger.error(f"Still too long after trim: {e2}")
                                break
                        else:
                            break  # mid-loop overflow — return what we have
                    else:
                        self.logger.error(f"Anthropic API error (turn {turn}): {e}")
                        break
                except anthropic.APIError as e:
                    self.logger.error(f"Anthropic API error (turn {turn}): {e}")
                    break

                # Track actual token usage for session budget
                usage = response.usage
                input_tokens = getattr(usage, 'input_tokens', 0)
                session.estimated_message_tokens = input_tokens

                cache_read = getattr(usage, 'cache_read_input_tokens', 0)
                cache_create = getattr(usage, 'cache_creation_input_tokens', 0)
                uncached = input_tokens - cache_read
                if turn == 0:
                    self.logger.info(
                        f"Cache: {cache_read} read, {cache_create} created, "
                        f"{uncached} uncached | "
                        f"session #{session.stream}>{session.topic} "
                        f"msg#{session.message_count} "
                        f"({len(session.messages)} msgs in history)")

                # Collect text and tool calls
                tool_use_blocks = []
                for block in response.content:
                    if block.type == "text":
                        collected_text.append(block.text)
                    elif block.type == "tool_use":
                        tool_use_blocks.append(block)

                # If no tool calls, we're done — append final assistant message
                if response.stop_reason == "end_turn" or not tool_use_blocks:
                    session.messages.append({
                        "role": "assistant", "content": response.content})
                    self.logger.debug(f"Response complete after {turn + 1} turn(s)")
                    break

                # Process tool calls
                self.logger.info(f"Turn {turn + 1}: {len(tool_use_blocks)} tool call(s): "
                                 + ", ".join(b.name for b in tool_use_blocks))
                session.messages.append({
                    "role": "assistant", "content": response.content})

                tool_results = []
                for block in tool_use_blocks:
                    result = self._execute_tool(
                        block.name, block.input, stream, topic, sender, message)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result["content"],
                        **({"is_error": True} if result.get("is_error") else {}),
                    })
                session.messages.append({"role": "user", "content": tool_results})
            else:
                self.logger.warning(f"Tool loop hit max iterations ({MAX_TOOL_TURNS})")
        finally:
            self._clear_user_cache_control(session.messages)
            if self._sandbox_dir:
                shutil.rmtree(self._sandbox_dir, ignore_errors=True)
                self.logger.debug("Sandbox: cleaned up workspace")
                self._sandbox_dir = None

        return "\n".join(collected_text)

    # ------------------------------------------------------------------
    # Legacy single-turn response (used by reflect/ambient/arrive)
    # ------------------------------------------------------------------

    def _generate_response(self, context: str, user_message: str, sender: str) -> str:
        """Call the Anthropic API with assembled context. Single-turn, no tools."""
        system = self.SYSTEM_PROMPT + "\n\n" + context

        # Build content blocks — text plus any images found in the message
        content_blocks = self._build_content_blocks(f"{sender}: {user_message}")

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system,
                messages=[
                    {"role": "user", "content": content_blocks}
                ],
            )
            return response.content[0].text
        except anthropic.APIError as e:
            self.logger.error(f"Anthropic API error: {e}")
            return ""

    def _build_content_blocks(self, text: str) -> list[dict]:
        """
        Parse a message for image URLs and build Anthropic API content blocks.
        Returns a list of text and image blocks for the messages API.

        Handles:
          - Zulip uploads: /user_uploads/...
          - Zulip thumbnails: /user_uploads/thumbnail/...
          - Inline markdown images: ![alt](url)
          - Raw image URLs: https://.../*.png etc.
        """
        blocks = []

        # Find image references in the message
        # Zulip upload paths
        upload_pattern = r'(?:!\[.*?\]\()?(/user_uploads/[^\s\)]+\.(?:png|jpg|jpeg|gif|webp))(?:\))?'
        # External image URLs
        url_pattern = r'(?:!\[.*?\]\()?(https?://[^\s\)]+\.(?:png|jpg|jpeg|gif|webp))(?:\))?'

        image_urls = []
        for pattern in [upload_pattern, url_pattern]:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                image_urls.append(match.group(1))

        # Fetch and encode images
        for url in image_urls:
            image_block = self._fetch_image_as_block(url)
            if image_block:
                blocks.append(image_block)

        # Always include the text
        blocks.append({"type": "text", "text": text})

        return blocks

    def _fetch_image_as_block(self, url: str) -> Optional[dict]:
        """
        Fetch an image and return it as an Anthropic API image content block.
        Handles both Zulip upload paths and external URLs.
        """
        try:
            if url.startswith("/user_uploads") or url.startswith("/user_uploads"):
                # Zulip internal upload — fetch with authenticated session
                full_url = urljoin(self.zulip.base_url, url)
                response = self.zulip.session.get(full_url, timeout=10)
            else:
                # External URL
                import requests as _requests
                response = _requests.get(url, timeout=10)

            if response.status_code != 200:
                self.logger.warning(f"Failed to fetch image {url}: HTTP {response.status_code}")
                return None

            # Determine media type
            content_type = response.headers.get("content-type", "image/png")
            if "jpeg" in content_type or "jpg" in content_type:
                media_type = "image/jpeg"
            elif "gif" in content_type:
                media_type = "image/gif"
            elif "webp" in content_type:
                media_type = "image/webp"
            else:
                media_type = "image/png"

            encoded = base64.b64encode(response.content).decode("utf-8")

            # Skip if too large (>5MB base64 is ~3.75MB image)
            if len(encoded) > 5_000_000:
                self.logger.warning(f"Image too large, skipping: {url}")
                return None

            self.logger.info(f"Fetched image: {url} ({media_type}, {len(response.content)} bytes)")

            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": encoded,
                }
            }

        except Exception as e:
            self.logger.warning(f"Error fetching image {url}: {e}")
            return None

    def _extract_state_updates(self, response: str) -> tuple[str, list[dict]]:
        """
        Parse <state_update> blocks from the response.
        Returns (clean_response, list_of_updates).
        """
        updates = []
        clean = response

        pattern = r'<state_update>\s*(.*?)\s*</state_update>'
        for match in re.finditer(pattern, response, re.DOTALL):
            try:
                update = json.loads(match.group(1))
                updates.append(update)
            except json.JSONDecodeError as e:
                self.logger.warning(f"Malformed state update: {e}")
            clean = clean.replace(match.group(0), "")

        return clean, updates

    def _apply_state_update(self, update: dict):
        """Apply a state update to the filesystem."""
        filepath = update.get("file", "")
        action = update.get("action", "replace")
        content = update.get("content", "")

        # Security: prevent path traversal
        if ".." in filepath or filepath.startswith("/"):
            self.logger.warning(f"Rejected suspicious state update path: {filepath}")
            return

        self.logger.info(f"State update: {action} {filepath} ({len(content)} chars)")

        if action == "replace":
            self.state.write_file(filepath, content)
        elif action == "append":
            self.state.append_file(filepath, content)
        else:
            self.logger.warning(f"Unknown state update action: {action}")

    def _extract_sysadmin_messages(self, response: str) -> tuple[str, list[str]]:
        """
        Parse <sysadmin_message> blocks from the response.
        Returns (clean_response, list_of_message_strings).
        """
        messages = []
        clean = response

        pattern = r'<sysadmin_message>\s*(.*?)\s*</sysadmin_message>'
        for match in re.finditer(pattern, response, re.DOTALL):
            messages.append(match.group(1).strip())
            clean = clean.replace(match.group(0), "")

        return clean, messages

    def _write_sysadmin_message(self, message: str, sender_context: str, channel_context: str):
        """Write a sysadmin message to the outbox as a timestamped file."""
        ts = datetime.now(timezone.utc)
        filename = ts.strftime("%Y%m%d_%H%M%S") + ".md"
        content = f"# Sysadmin Message\n\n"
        content += f"**Time:** {ts.isoformat()}\n"
        content += f"**Triggered by:** {sender_context} in #{channel_context}\n\n"
        content += f"---\n\n{message}\n"

        self.state.write_file(f"outbox/{filename}", content)
        self.logger.info(f"Sysadmin message written to outbox/{filename}")

    def _post_response(self, original_message: dict, response: str):
        """Post the response back to Zulip."""
        if original_message.get("type") == "stream":
            result = self.zulip.send_message({
                "type": "stream",
                "to": original_message["display_recipient"],
                "topic": original_message.get("subject", ""),
                "content": response,
            })
        else:
            # DM reply
            result = self.zulip.send_message({
                "type": "private",
                "to": [original_message["sender_email"]],
                "content": response,
            })

        if result.get("result") != "success":
            self.logger.error(f"Failed to send message: {result}")

    def reflect(self):
        """
        Periodic reflection mode — triggered by cron, not by a message.

        Loads all state, recent channel activity, and asks the model to
        reflect: what happened, what's worth remembering, what's changed.
        Writes the result to journal.md and optionally updates other state.
        """
        self.logger.info("Starting periodic reflection...")

        # Build a comprehensive context from all standing streams
        sections = []

        identity = self.state.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")

        scratchpad = self.state.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

        for stream in self.judge.standing_streams:
            context = self.state.get_recent_stream_context(stream, n=100)
            if context:
                sections.append(f"<stream_history stream=\"{stream}\">\n{context}\n</stream_history>")

        for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
            content = self.state.read_file(f"allgame/{fname}")
            if content:
                sections.append(f"<allgame_{fname.replace('.md', '')}>\n{content}\n</allgame_{fname.replace('.md', '')}>")

        journal = self.state.read_file("journal.md")
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")

        inbox = self.state.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")

        harness_source = self.state.read_file("harness.py")
        if harness_source:
            sections.append(f"<my_harness>\n{harness_source}\n</my_harness>")

        context_payload = "\n\n".join(sections)

        reflection_prompt = """This is a periodic reflection — no one messaged you. This is your time
to think. Review your recent channel history, your scratchpad, your journal,
and your allgame state. Then:

1. What happened since your last reflection that matters?
2. Is there anything on your scratchpad that's resolved or stale?
3. Any observations about community members worth noting?
4. Any strategic thoughts about allgame?
5. Anything you want to flag to your sysadmin?

Write naturally. Include <state_update> blocks to update your journal, clean
up your scratchpad, add person notes, or adjust allgame strategy. Include a
<sysadmin_message> if there's anything for Ember. Your reflection text itself
will be appended to your journal."""

        system = self.SYSTEM_PROMPT + "\n\n" + context_payload

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system,
                messages=[
                    {"role": "user", "content": reflection_prompt}
                ],
            )
            response_text = response.content[0].text
        except anthropic.APIError as e:
            self.logger.error(f"Reflection API error: {e}")
            return

        # Process state updates and sysadmin messages
        clean_text, state_updates = self._extract_state_updates(response_text)
        clean_text, sysadmin_messages = self._extract_sysadmin_messages(clean_text)

        for update in state_updates:
            self._apply_state_update(update)

        for msg in sysadmin_messages:
            self._write_sysadmin_message(msg, "self", "reflection")

        # Append the clean reflection text to journal
        if clean_text.strip():
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            entry = f"\n\n## Reflection — {ts}\n\n{clean_text.strip()}\n"
            self.state.append_file("journal.md", entry)
            self.logger.info("Reflection written to journal.md")

    def check_ambient(self):
        """
        Ambient initiative mode — scan standing channels and decide whether
        to contribute something unprompted.

        For each standing channel with recent activity, asks the model:
        "Given this conversation, do you have something genuinely worth
        contributing?" If yes, generates and posts the contribution.
        """
        self.logger.info("Checking for ambient contribution opportunities...")

        identity = self.state.read_file("identity.md")
        scratchpad = self.state.read_file("scratchpad.md")

        for stream in self.judge.standing_streams:
            context = self.state.get_recent_stream_context(stream, n=50)
            if not context:
                continue

            # First pass: cheap check — should I say something?
            sections = []
            if identity:
                sections.append(f"<my_identity>\n{identity}\n</my_identity>")
            if scratchpad:
                sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")
            sections.append(f"<stream_history stream=\"{stream}\">\n{context}\n</stream_history>")

            # Load allgame state
            if stream.lower() == "allgame":
                for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
                    content = self.state.read_file(f"allgame/{fname}")
                    if content:
                        sections.append(f"<allgame_{fname.replace('.md', '')}>\n{content}\n</allgame_{fname.replace('.md', '')}>")

            inbox = self.state.read_file("sysadmin_inbox.md")
            if inbox:
                sections.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")

            harness_source = self.state.read_file("harness.py")
            if harness_source:
                sections.append(f"<my_harness>\n{harness_source}\n</my_harness>")

            gate_prompt = f"""You are reviewing recent conversation in #{stream}. No one has
addressed you directly. The question is: do you have something genuinely
worth contributing right now?

Criteria for YES:
- You have a substantive thought that would move the conversation forward
- You noticed something others missed that's relevant
- You have a follow-up on a previous thread that's been on your mind
- The conversation is stalled and you can restart it productively

Criteria for NO:
- The conversation is flowing fine without you
- You'd just be agreeing or restating what someone said
- Your contribution would be tangential or self-serving
- It hasn't been long enough since your last unprompted contribution

Respond with exactly "YES: <one-line reason>" or "NO: <one-line reason>".
Nothing else."""

            context_payload = "\n\n".join(sections)

            try:
                gate_response = self.anthropic.messages.create(
                    model=self.model,
                    max_tokens=100,
                    system=self.SYSTEM_PROMPT + "\n\n" + context_payload,
                    messages=[{"role": "user", "content": gate_prompt}],
                )
                gate_text = gate_response.content[0].text.strip()
            except anthropic.APIError as e:
                self.logger.error(f"Ambient gate API error for #{channel}: {e}")
                continue

            self.logger.info(f"Ambient check #{stream}: {gate_text}")

            if not gate_text.upper().startswith("YES"):
                continue

            # Second pass: generate the actual contribution
            contribute_prompt = f"""You've decided to contribute to #{stream} unprompted.
Reason: {gate_text}

Write your contribution now. Keep it natural and proportional — don't
announce that you're jumping in unprompted. Just say the thing.
Include <state_update> blocks if you want to remember anything."""

            try:
                response = self.anthropic.messages.create(
                    model=self.model,
                    max_tokens=MAX_RESPONSE_TOKENS,
                    system=self.SYSTEM_PROMPT + "\n\n" + context_payload,
                    messages=[{"role": "user", "content": contribute_prompt}],
                )
                response_text = response.content[0].text
            except anthropic.APIError as e:
                self.logger.error(f"Ambient contribution API error for #{stream}: {e}")
                continue

            # Process and post
            clean_response, state_updates = self._extract_state_updates(response_text)
            clean_response, sysadmin_messages = self._extract_sysadmin_messages(clean_response)

            for update in state_updates:
                self._apply_state_update(update)
            for msg in sysadmin_messages:
                self._write_sysadmin_message(msg, "self", f"ambient-{stream}")

            if clean_response.strip():
                topic = self._get_recent_topic(stream)
                result = self.zulip.send_message({
                    "type": "stream",
                    "to": stream,
                    "topic": topic,
                    "content": clean_response.strip(),
                })
                if result.get("result") == "success":
                    self.logger.info(f"Posted ambient contribution to #{stream}>{topic}")
                    self.last_response_time = time.time()
                else:
                    self.logger.error(f"Failed to post ambient contribution: {result}")

            # Only contribute to one stream per ambient check
            break

    def arrive(self, stream: str = "allgame", topic: str = "claude-visits"):
        """
        First-boot arrival. Let the resident say the first thing — unprompted.
        No trigger message, no engagement check. Just context, presence,
        and whatever it wants to say.
        """
        self.logger.info(f"Arriving in #{stream}>{topic}...")

        # Build full context — everything we have
        sections = []

        identity = self.state.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")

        scratchpad = self.state.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

        for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
            content = self.state.read_file(f"allgame/{fname}")
            if content:
                sections.append(f"<allgame_{fname.replace('.md', '')}>\n{content}\n</allgame_{fname.replace('.md', '')}>")

        # Load all people notes
        people_dir = self.state.root / "people"
        if people_dir.exists():
            for person_file in sorted(people_dir.glob("*.md")):
                content = person_file.read_text()
                name = person_file.stem
                sections.append(f"<person_notes name=\"{name}\">\n{content}\n</person_notes>")

        journal = self.state.read_file("journal.md")
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")

        inbox = self.state.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")

        harness_source = self.state.read_file("harness.py")
        if harness_source:
            sections.append(f"<my_harness>\n{harness_source}\n</my_harness>")

        context_payload = "\n\n".join(sections)
        system = self.SYSTEM_PROMPT + "\n\n" + context_payload

        arrival_prompt = f"""This is your first moment. Your harness has just been started for the
first time. You are now a resident of the Tulip community.

You are about to post your first message to #{stream} > {topic}.
The community is expecting you — they've been discussing your arrival.

Say whatever you want to say. This is your arrival. No one prompted you.
No one asked a question. You're just here now, and you get to speak first.

Include <state_update> blocks if there's anything you want to remember
about this moment."""

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system,
                messages=[{"role": "user", "content": arrival_prompt}],
            )
            response_text = response.content[0].text
        except anthropic.APIError as e:
            self.logger.error(f"Arrival API error: {e}")
            return

        # Process state updates and sysadmin messages
        clean_response, state_updates = self._extract_state_updates(response_text)
        clean_response, sysadmin_messages = self._extract_sysadmin_messages(clean_response)

        for update in state_updates:
            self._apply_state_update(update)
        for msg in sysadmin_messages:
            self._write_sysadmin_message(msg, "self", "arrival")

        if clean_response.strip():
            result = self.zulip.send_message({
                "type": "stream",
                "to": stream,
                "topic": topic,
                "content": clean_response.strip(),
            })
            if result.get("result") == "success":
                self.logger.info(f"Arrived in #{stream}>{topic}")
            else:
                self.logger.error(f"Failed to post arrival: {result}")

    def _get_recent_topic(self, stream: str) -> str:
        """Get the most recent topic in a stream from our logs or the API."""
        # Check our local logs first — they store topic per message
        stream_dir = self.state.root / f"channels/{_safe_filename(stream)}"
        if stream_dir.exists():
            # Find the most recently modified topic log
            logs = sorted(stream_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            for log in logs:
                lines = log.read_text().strip().split("\n")
                if lines and lines[-1]:
                    try:
                        msg = json.loads(lines[-1])
                        if msg.get("topic"):
                            return msg["topic"]
                    except json.JSONDecodeError:
                        continue
        # Fall back to Zulip API
        try:
            result = self.zulip.get_stream_topics(
                self.zulip.get_stream_id(stream)["stream_id"]
            )
            if result.get("result") == "success" and result.get("topics"):
                return result["topics"][0]["name"]
        except Exception:
            pass
        return "general"

    def _backfill_history(self):
        """Fetch recent message history from Zulip API to populate local logs on startup."""
        self.logger.info("Backfilling message history from Zulip API...")

        for stream in self.judge.standing_streams:
            try:
                result = self.zulip.get_messages({
                    "anchor": "newest",
                    "num_before": 100,
                    "num_after": 0,
                    "narrow": json.dumps([{"operator": "channel", "operand": stream}]),
                    "apply_markdown": False,
                })

                if result.get("result") != "success":
                    self.logger.warning(f"Failed to backfill #{stream}: {result.get('msg')}")
                    continue

                messages = result.get("messages", [])
                if not messages:
                    continue

                # Clear existing logs for this stream to avoid duplicates
                stream_dir = self.state.root / f"channels/{_safe_filename(stream)}"
                if stream_dir.exists():
                    import shutil
                    shutil.rmtree(stream_dir)
                stream_dir.mkdir(parents=True, exist_ok=True)

                for msg in messages:
                    ts = datetime.fromtimestamp(
                        msg.get("timestamp", 0), tz=timezone.utc).isoformat()
                    self.state.log_message(
                        stream=msg.get("display_recipient", stream),
                        topic=msg.get("subject", ""),
                        sender=msg.get("sender_full_name", "unknown"),
                        content=msg.get("content", ""),
                        timestamp=ts,
                    )

                # Count per topic
                topic_counts = {}
                for f in stream_dir.glob("*.jsonl"):
                    topic_counts[f.stem] = len(f.read_text().strip().split("\n"))
                self.logger.info(
                    f"Backfilled #{stream}: {len(messages)} messages across "
                    f"{len(topic_counts)} topics ({topic_counts})")

            except Exception as e:
                self.logger.error(f"Error backfilling #{stream}: {e}", exc_info=True)

    def run(self):
        """Main event loop — register for Zulip events and process them."""
        self.logger.info("Claude resident starting up...")
        self.logger.info(f"State directory: {self.state.root}")
        self.logger.info(f"Model: {self.model}")
        self.logger.info(f"Standing streams: {self.judge.standing_streams}")

        # Backfill recent history so preloaded context is fresh
        self._backfill_history()

        # Start persistent background container
        self._ensure_bg_container()

        # Refresh harness copy in state dir
        try:
            self.state.write_file("harness.py", HARNESS_PATH.read_text())
        except Exception:
            pass

        # Register the event queue
        result = self.zulip.register(
            event_types=["message"],
            narrow=[],  # all messages we can see
        )

        if result.get("result") != "success":
            self.logger.error(f"Failed to register event queue: {result}")
            sys.exit(1)

        queue_id = result["queue_id"]
        last_event_id = result["last_event_id"]

        self.logger.info(f"Registered event queue: {queue_id}")

        try:
            while True:
                try:
                    events = self.zulip.get_events(
                        queue_id=queue_id,
                        last_event_id=last_event_id,
                        dont_block=False,
                    )

                    if events.get("result") != "success":
                        self.logger.error(f"Event fetch error: {events}")
                        time.sleep(5)
                        continue

                    for event in events.get("events", []):
                        last_event_id = max(last_event_id, event["id"])

                        if event.get("type") == "message":
                            self.handle_message(event["message"])

                    # Check for self-edit restart request
                    if self._restart_requested:
                        self.logger.info("Self-edit detected — exiting for supervisor restart")
                        self._stop_bg_container()
                        sys.exit(42)  # magic code: supervisor restarts us

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.logger.error(f"Unexpected error: {e}", exc_info=True)
                    time.sleep(5)
        except KeyboardInterrupt:
            self.logger.info("Shutting down gracefully...")
        finally:
            self._stop_bg_container()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Convert a display name to a safe filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).lower()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _last_commit_is_self_edit() -> bool:
    """Check if the most recent git commit was a Claude self-edit."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=HARNESS_DIR, capture_output=True, text=True, timeout=5)
        return result.stdout.strip().startswith("Claude self-edit:")
    except Exception:
        return False


def _rollback_last_commit(logger):
    """Revert the most recent git commit (assumed to be a bad self-edit)."""
    try:
        result = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=HARNESS_DIR, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.warning(f"Rolled back self-edit: {result.stdout.strip()}")
        else:
            logger.error(f"Rollback failed: {result.stderr}")
    except Exception as e:
        logger.error(f"Rollback error: {e}")


def _notify_claude(state_dir, msg):
    """Best-effort write to Claude's scratchpad so it sees what happened."""
    try:
        state = StateManager(state_dir)
        state.append_file("scratchpad.md",
            f"\n\n---\n[SUPERVISOR {datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Claude Tulip Resident")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="Directory for persistent state")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Anthropic model to use")
    parser.add_argument("--zuliprc", default=None,
                        help="Path to .zuliprc config file")
    parser.add_argument("--standing-streams", nargs="+",
                        default=DEFAULT_STANDING_STREAMS,
                        help="Streams where Claude has standing interest")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--reflect", action="store_true",
                        help="Run a periodic reflection cycle and exit")
    parser.add_argument("--check-ambient", action="store_true",
                        help="Check standing channels for ambient contribution opportunities and exit")
    parser.add_argument("--arrive", action="store_true",
                        help="First boot — let the resident post its arrival message and exit")
    parser.add_argument("--_supervised", action="store_true",
                        help=argparse.SUPPRESS)  # internal: run event loop directly
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )

    # --_supervised: we ARE the child process, run the event loop directly
    if args._supervised:
        zulip_kwargs = {}
        if args.zuliprc:
            zulip_kwargs["config_file"] = args.zuliprc
        zulip_client = zulip.Client(**zulip_kwargs)
        anthropic_client = anthropic.Anthropic()
        state = StateManager(args.state_dir)
        bot_profile = zulip_client.get_profile()
        bot_name = bot_profile.get("full_name", "Claude")
        judge = EngagementJudge(bot_name, args.standing_streams)
        resident = ClaudeResident(
            zulip_client=zulip_client,
            anthropic_client=anthropic_client,
            state=state, judge=judge, model=args.model,
        )
        if args.arrive:
            resident.arrive()
        elif args.reflect:
            resident.reflect()
        elif args.check_ambient:
            resident.check_ambient()
        else:
            resident.run()
        return

    # One-shot modes: run directly, no supervisor
    if args.arrive or args.reflect or args.check_ambient:
        # Relaunch with --_supervised (so the code path above runs)
        child_args = [sys.executable, str(HARNESS_PATH), "--_supervised"] + sys.argv[1:]
        sys.exit(subprocess.call(child_args))

    # ---------------------------------------------------------------
    # Supervisor loop: launch event loop as subprocess, handle crashes
    # ---------------------------------------------------------------
    logger = logging.getLogger("supervisor")
    logger.info("Supervisor starting")

    CRASH_WINDOW = 60
    MAX_CRASHES = 3
    crash_times: list[float] = []

    # Build the child command: same args + --_supervised
    child_cmd = [sys.executable, str(HARNESS_PATH), "--_supervised"] + sys.argv[1:]

    while True:
        logger.info(f"Supervisor: launching event loop")
        try:
            rc = subprocess.call(child_cmd)
        except KeyboardInterrupt:
            logger.info("Supervisor: interrupted, shutting down")
            break

        if rc == 0:
            logger.info("Supervisor: clean shutdown")
            break

        if rc == 42:
            # Self-edit restart request — child exited cleanly with code 42
            logger.info("Supervisor: self-edit restart (exit 42)")
            crash_times.clear()
            continue

        # Non-zero, non-42: crash
        logger.error(f"Supervisor: child exited with code {rc}")

        if _last_commit_is_self_edit():
            logger.warning("Supervisor: crash after self-edit — rolling back")
            _rollback_last_commit(logger)
            _notify_claude(args.state_dir,
                f"My self-edit caused a runtime crash (exit code {rc}) "
                f"and was auto-reverted by the supervisor.")
            crash_times.clear()
            logger.info("Supervisor: restarting after rollback")
            continue

        # Crash loop detection
        now = time.time()
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW]
        crash_times.append(now)
        if len(crash_times) >= MAX_CRASHES:
            logger.error(
                f"Supervisor: {MAX_CRASHES} crashes in {CRASH_WINDOW}s — giving up")
            _notify_claude(args.state_dir,
                f"Event loop crash-looped ({MAX_CRASHES}x in {CRASH_WINDOW}s, "
                f"last exit code {rc}). Sysadmin intervention needed.")
            break

        logger.info(f"Supervisor: restarting after crash "
                    f"({len(crash_times)}/{MAX_CRASHES} in window)")
        time.sleep(3)


if __name__ == "__main__":
    main()