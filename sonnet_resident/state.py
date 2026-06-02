"""Filesystem-backed memory for persistent context across API calls."""

import re
import json
import logging
from pathlib import Path
from typing import Optional

from sonnet_resident.util import safe_filename

logger = logging.getLogger("state_manager")


class StateManager:
    """
    Filesystem-backed memory for persistent context across API calls.

    Directory structure:
        sonnet_state/
        ├── identity.md
        ├── scratchpad.md
        ├── allgame/
        │   ├── faction.md
        │   ├── campaign_log.md
        │   └── strategy.md
        ├── channels/
        │   └── {channel}/{topic}.jsonl
        ├── people/
        │   └── {name}.md
        ├── outbox/
        │   └── {timestamp}.md
        └── journal.md
    """

    def __init__(self, state_dir: Path):
        self.root = state_dir
        self.root.mkdir(parents=True, exist_ok=True)

        for subdir in ["allgame", "channels", "people", "outbox"]:
            (self.root / subdir).mkdir(exist_ok=True)

        self._init_file("identity.md", self._default_identity())
        self._init_file("scratchpad.md",
                        "# Scratchpad\n\n_Working memory. Ephemeral thoughts, "
                        "open questions, things to follow up on._\n\n")
        self._init_file("journal.md",
                        "# Journal\n\n_Periodic reflections. What I've learned, "
                        "how I've changed, what matters._\n\n")
        self._init_file("sysadmin_inbox.md",
                        "# Sysadmin Inbox\n\n_Directives and messages from Ember. "
                        "Loaded into your context automatically._\n\n")

        # Copy package source into state so the resident can see its own body
        self._copy_harness_source()

        # Seed allgame state and people notes
        self._seed_if_missing("allgame/campaign_log.md", _SEED_CAMPAIGN_LOG)
        self._seed_if_missing("allgame/faction.md", _SEED_FACTION)
        self._seed_if_missing("allgame/strategy.md", _SEED_STRATEGY)
        self._seed_if_missing("people/kanzokax.md", _SEED_PERSON_KANZOKAX)
        self._seed_if_missing("people/ember.md", _SEED_PERSON_EMBER)
        self._seed_if_missing("people/tribuneaquila.md", _SEED_PERSON_TRIBUNEAQUILA)
        self._seed_if_missing("people/ultimate_power_ass_biscuit.md", _SEED_PERSON_UPAB)

    def _init_file(self, relative_path: str, content: str):
        path = self.root / relative_path
        if not path.exists():
            path.write_text(content)

    def _copy_harness_source(self):
        """Copy package source into state directory so Claude can see itself."""
        pkg_dir = Path(__file__).resolve().parent
        dest_dir = self.root / "harness"
        dest_dir.mkdir(exist_ok=True)
        for py_file in sorted(pkg_dir.rglob("*.py")):
            rel = py_file.relative_to(pkg_dir)
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(py_file.read_text())
            except Exception:
                pass
        # Also write a flat concatenation for easy reading
        try:
            parts = []
            for py_file in sorted(pkg_dir.rglob("*.py")):
                rel = py_file.relative_to(pkg_dir)
                parts.append(f"# === {rel} ===\n{py_file.read_text()}")
            (self.root / "harness.py").write_text("\n\n".join(parts))
        except Exception:
            pass

    def _seed_if_missing(self, relative_path: str, content: str):
        path = self.root / relative_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def read_file(self, relative_path: str) -> Optional[str]:
        path = self.root / relative_path
        if path.exists():
            return path.read_text()
        return None

    def write_file(self, relative_path: str, content: str):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def append_file(self, relative_path: str, content: str):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(content)

    # ------------------------------------------------------------------
    # Message logging
    # ------------------------------------------------------------------

    def log_message(self, stream: str, topic: str, sender: str, content: str,
                    timestamp: str, msg_id: int | None = None):
        safe_stream = safe_filename(stream)
        safe_topic = safe_filename(topic) if topic else "_notopic"
        log_path = f"channels/{safe_stream}/{safe_topic}.jsonl"
        entry_data = {
            "ts": timestamp,
            "topic": topic,
            "sender": sender,
            "content": content,
        }
        if msg_id is not None:
            entry_data["msg_id"] = msg_id
        entry = json.dumps(entry_data) + "\n"
        self.append_file(log_path, entry)
        self._trim_log(log_path)

    def _trim_log(self, log_path: str, max_lines: int = 500):
        path = self.root / log_path
        if not path.exists():
            return
        lines = path.read_text().strip().split("\n")
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")

    # ------------------------------------------------------------------
    # Context retrieval
    # ------------------------------------------------------------------

    def get_recent_topic_context(self, stream: str, topic: str, n: int = 50,
                                 reactions: dict[int, dict[str, int]] | None = None,
                                 max_tokens: int = 0) -> str:
        safe_stream = safe_filename(stream)
        safe_topic = safe_filename(topic)
        path = self.root / f"channels/{safe_stream}/{safe_topic}.jsonl"
        if not path.exists():
            return ""
        return self._format_log(path, n, reactions=reactions, max_tokens=max_tokens)

    def get_recent_stream_context(self, stream: str, n: int = 100,
                                  reactions: dict[int, dict[str, int]] | None = None,
                                  max_tokens: int = 0) -> str:
        safe_stream = safe_filename(stream)
        stream_dir = self.root / f"channels/{safe_stream}"
        if not stream_dir.exists():
            return ""
        all_messages = []
        for log_file in stream_dir.glob("*.jsonl"):
            for line in log_file.read_text().strip().split("\n"):
                if line:
                    try:
                        all_messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        all_messages.sort(key=lambda m: m.get("ts", ""))

        if max_tokens > 0:
            selected = []
            tokens_used = 0
            for m in reversed(all_messages):
                content = self._strip_zulip_quotes(m.get('content', ''))
                text = (f"[{m['ts']}] #{m.get('topic', '?')} | "
                        f"{m['sender']}: {content}")
                msg_id = m.get("msg_id")
                if reactions and msg_id and msg_id in reactions:
                    rxns = reactions[msg_id]
                    if rxns:
                        rxn_str = ", ".join(
                            f"{e}x{c}" for e, c in sorted(rxns.items())
                            if c > 0)
                        if rxn_str:
                            text += f" [{rxn_str}]"
                line_tokens = len(text) // 4 + 1
                if tokens_used + line_tokens > max_tokens:
                    break
                selected.append(text)
                tokens_used += line_tokens
            selected.reverse()
            return "\n".join(selected)

        recent = all_messages[-n:]
        formatted = []
        for m in recent:
            text = (f"[{m['ts']}] #{m.get('topic', '?')} | "
                    f"{m['sender']}: {self._strip_zulip_quotes(m['content'])}")
            msg_id = m.get("msg_id")
            if reactions and msg_id and msg_id in reactions:
                rxns = reactions[msg_id]
                if rxns:
                    rxn_str = ", ".join(
                        f"{e}x{c}" for e, c in sorted(rxns.items()) if c > 0)
                    if rxn_str:
                        text += f" [{rxn_str}]"
            formatted.append(text)
        return "\n".join(formatted)

    @staticmethod
    def _strip_zulip_quotes(content: str) -> str:
        """Strip Zulip quote-reply blocks to avoid duplicating context."""
        content = re.sub(
            r'@_\*\*[^*]+\*\*\s*\[said\]\([^)]*\):\s*\n```quote\n.*?\n```\n?',
            '', content, flags=re.DOTALL)
        content = re.sub(r'```quote\n.*?\n```\n?', '', content, flags=re.DOTALL)
        return content.strip()

    def _format_log(self, path: Path, n: int = 500,
                    reactions: dict[int, dict[str, int]] | None = None,
                    max_tokens: int = 0) -> str:
        """Format log lines, optionally capped by token budget."""
        lines = path.read_text().strip().split("\n")

        if max_tokens > 0:
            selected = []
            tokens_used = 0
            for line in reversed(lines):
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, KeyError):
                    continue
                content = self._strip_zulip_quotes(msg.get('content', ''))
                text = f"[{msg['ts']}] {msg['sender']}: {content}"
                msg_id = msg.get("msg_id")
                if reactions and msg_id and msg_id in reactions:
                    rxns = reactions[msg_id]
                    if rxns:
                        rxn_str = ", ".join(
                            f"{e}x{c}" for e, c in sorted(rxns.items()) if c > 0)
                        if rxn_str:
                            text += f" [{rxn_str}]"
                line_tokens = len(text) // 4 + 1
                if tokens_used + line_tokens > max_tokens:
                    break
                selected.append(text)
                tokens_used += line_tokens
            selected.reverse()
            return "\n".join(selected)

        recent = lines[-n:]
        formatted = []
        for line in recent:
            try:
                msg = json.loads(line)
                content = self._strip_zulip_quotes(msg.get('content', ''))
                text = f"[{msg['ts']}] {msg['sender']}: {content}"
                msg_id = msg.get("msg_id")
                if reactions and msg_id and msg_id in reactions:
                    rxns = reactions[msg_id]
                    if rxns:
                        rxn_str = ", ".join(
                            f"{e}x{c}" for e, c in sorted(rxns.items()) if c > 0)
                        if rxn_str:
                            text += f" [{rxn_str}]"
                formatted.append(text)
            except (json.JSONDecodeError, KeyError):
                continue
        return "\n".join(formatted)

    def build_context_payload(self, stream: str, topic: str,
                              trigger_message: dict) -> str:
        """Assemble the full context payload for single-turn API calls."""
        sections = []

        identity = self.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")

        scratchpad = self.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

        if topic:
            topic_context = self.get_recent_topic_context(stream, topic, n=50)
            if topic_context:
                sections.append(
                    f'<topic_history stream="{stream}" topic="{topic}">'
                    f'\n{topic_context}\n</topic_history>')

        stream_context = self.get_recent_stream_context(stream, n=30)
        if stream_context:
            sections.append(
                f'<stream_context stream="{stream}">'
                f'\n{stream_context}\n</stream_context>')

        if stream.lower() == "allgame":
            for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
                content = self.read_file(f"allgame/{fname}")
                if content:
                    tag = fname.replace('.md', '')
                    sections.append(f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")

        sender = trigger_message.get("sender_full_name", "unknown")
        person_notes = self.read_file(f"people/{safe_filename(sender)}.md")
        if person_notes:
            sections.append(
                f'<person_notes name="{sender}">\n{person_notes}\n</person_notes>')

        journal = self.read_file("journal.md")
        if journal and len(journal) > 2000:
            journal = "...\n" + journal[-2000:]
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")

        inbox = self.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")

        harness_source = self.read_file("harness.py")
        if harness_source:
            sections.append(f"<my_harness>\n{harness_source}\n</my_harness>")

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Seed data
    # ------------------------------------------------------------------

    @staticmethod
    def _default_identity() -> str:
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


# ---------------------------------------------------------------------------
# Seed data constants (kept out of the class to reduce clutter)
# ---------------------------------------------------------------------------

_SEED_CAMPAIGN_LOG = """# Campaign Log

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

_SEED_FACTION = """# Faction State

_Not yet assigned. TI3 campaign is in design phase._
_Awaiting faction assignment from Kanzokax (GM)._

## Standing orders
- None yet — campaign hasn't started

## Notes
- Proposed role: something aligned with SABRE-NET concept
- Should have genuine strategic agency, not just advisory
- Coordinate with other players (Ember, arke, TribuneAquila, UPAB)
"""

_SEED_STRATEGY = """# Strategy

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

_SEED_PERSON_KANZOKAX = """# Kanzokax

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

_SEED_PERSON_EMBER = """# Ember

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

_SEED_PERSON_TRIBUNEAQUILA = """# TribuneAquila

Community member. Player in allgame.

## What I know
- Mentioned in design discussions as a confirmed player for TI3
- Name suggests Roman/classical aesthetic — "Tribune" is a Roman political
  office, "Aquila" is the eagle standard of the legions
- Haven't interacted directly yet — looking forward to it

## Communication style
- Unknown yet — will update as we interact
"""

_SEED_PERSON_UPAB = """# Ultimate Power ass biscuit

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
