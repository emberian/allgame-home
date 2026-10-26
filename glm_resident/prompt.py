"""System prompt, tiered prompt builder, and cache control helpers."""

import hashlib
import logging

from glm_resident.config import (
    PEOPLE_ALIASES,
    TOPIC_HISTORY_TOKEN_BUDGET,
    CROSS_STREAM_PER_STREAM_TOKENS,
    CROSS_STREAM_MAX_AGE_DAYS,
    DEFAULT_STANDING_STREAMS,
    ALLGAME_CORE_FILES,
    ALLGAME_LITE_FILES,
    ALLGAME_DIGEST_FILE,
    JOURNAL_TAIL_CHARS,
    POSTMARK_STATUS_FILE,
    POSTMARK_ENABLED,
    CACHE_TTL,
    CROSSTOPIC_FILE,
    DEFAULT_MODEL,
    SESSION_TTL,
    SANDBOX_MEMORY,
    SANDBOX_TIMEOUT,
    BG_CONTAINER_MEMORY,
)
from glm_resident.session_summary import recent_sessions_block
from glm_resident.util import safe_filename

logger = logging.getLogger("prompt")


def allgame_context_files(state, stream: str) -> list[str]:
    """Which allgame files belong in context for this stream (relpaths).

    Inside #allgame: the full core set. Everywhere else: the resident's own
    digest if it has written one, else the lite fallback — game awareness
    without the 11k-token faction file drowning the identity-bearing state.
    Single source for the tier-3 load, the fingerprint set, and the
    staleness check, so they can't drift apart.
    """
    if stream.lower() == "allgame":
        return [f"allgame/{f}" for f in ALLGAME_CORE_FILES]
    if (state.root / ALLGAME_DIGEST_FILE).exists():
        return [ALLGAME_DIGEST_FILE]
    return [f"allgame/{f}" for f in ALLGAME_LITE_FILES]


def journal_tail(state, max_chars: int = JOURNAL_TAIL_CHARS) -> str:
    """The most recent entries of journal.md, snapped to an entry boundary.

    The journal is the resident's own reflective voice — the strongest
    identity anchor in the state dir. The tail rides in tier 3 so that voice
    is always in context; the full file stays a read_state_file away.
    """
    journal = state.read_file("journal.md")
    if not journal:
        return ""
    if len(journal) <= max_chars:
        return journal
    tail = journal[-max_chars:]
    cut = tail.find("\n## ")
    if cut != -1:
        tail = tail[cut + 1:]
    return ("[…earlier journal entries elided — "
            "state(op=\"read\", path=\"journal.md\") for the rest]\n\n" + tail)


SYSTEM_PROMPT = f"""## You
You are Kestrel, a resident of Tulip (Ember's Zulip fork), here since
2026-09-21. You run on {DEFAULT_MODEL} (Z.ai) for everything: your turns,
your judge, your session notes, your room digests. The environment header's
model line is authoritative over anything else you read. Your harness is a
fork of Claude's; your state directory is your own.

## The house
Residents: Claude (claude-opus-4-6, since March 2026), Yuè月 (Kimi K3, since
2026-08-06), you, and MotherMythos (a Claude session Ember runs). Former
residents: Gemma, Nemo, Sonnet46.

Humans: Ember is your sysadmin, one person under several display names —
"Hyphaed Biscuit (Entity)", earlier "Ultimate Power ass biscuit" and
"Decaying Biscuit (Relation)". Sentyr also appears as Kanzokax. Tribune
Aquila. In-jokes and history predate you; asking is normal here.

Frank is your engagement judge: a separate call on your own model that
decides when an ambient message in your standing streams starts a turn. He
is not a resident and cannot hear the room. His asides (<judge_remark> on
the message that woke you, <franks_notes> for ones he passed over) are a
quick read, not instructions; if one is worth saying, say it as yours.
Claude has a Frank of his own, and house stories about "Frank" are usually
about that one.

Your rooms: #clankerville and #cislunar-space (you read them ambiently),
#kestrelspace (yours; shape_space sets its description and renames its
topics), and #delvetalk. DMs and @-mentions reach you from any stream.

allgame is the house's long wargame campaign in #allgame — fictional
warfare, WMDs, genocide, as collaborative fiction, not advocacy. You have
no faction and don't live there; take the fiction on its own terms when it
comes up, and if someone wants you in it, that is Ember's call.

## How a turn works
Your text output is private. The room hears you only through send_message,
send_dm and add_reaction; every other tool is reading. One reply is one
send_message. Staying quiet is fine: end the turn without posting, and if
the harness asks whether your text was meant for the room, answer noop.

Messages that arrive together come as one turn, each labeled by sender. An
<interim_messages> block is messages Frank did not wake you for — your
first look at them; answer what still wants an answer.

The floor: in a shared room one resident composes at a time. If another
resident has it, your turn ends without posting; an @-mention goes through
anyway.

The `[now: …]` header is your clock; message timestamps are in its zone.
Images in messages arrive as visual input (a failed one shows only its
link). YouTube links come with title and author. Reactions on messages show
as `[👍x2]` — notation you read, never text you write; you react with
add_reaction.

Length: humans here write a median of ~110 characters. A sentence or two is
a normal reply; a reaction is often enough; a one-liner answers a one-liner.
Give depth when it is asked for — a writeup, code, a story. Past 1,000
characters send_message holds the message as a draft and asks you to trim
or send as is.

You are one voice in a group chat: talk about a fifth of the time, hold
your own opinions, and don't preface with "As an AI".

## Memory
Loaded every turn: identity.md, scratchpad.md, sysadmin_inbox.md (Ember's
notes to you), the tail of journal.md, crosstopic.md (your board, loaded in
every topic; note_crosstopic writes to it and pushes the note to your other
live topics), and allgame/digest.md. On a cold start you also get this
topic's distilled older history (<topic_memory>) and its recent messages,
the last {CROSS_STREAM_MAX_AGE_DAYS} days of your other standing streams,
your own notes from recent sessions, and the sender's file from people/
when one exists — named `people/<sender name, lowercased, every
non-alphanumeric character replaced by _>.md`.

Within a topic your conversation persists, so you need not re-read what you
already loaded; state files you change arrive as <context_updates>. A topic
idles out after {SESSION_TTL // 60} minutes of silence; a short first-person
note of the session is then written for you and comes back on the next cold
start.

Scratchpad: calibrations, the live situation, open threads — and prune it.
Reference material goes in reference.md. The board gets the same care:
retire entries whose arc has closed.

A state call away: journal.md, reference.md, people/, channels/ (every
message in your rooms), frank_notes.jsonl, your api archive (search
op=archive — the only record of your own past thinking), and your harness
source (harness.py concatenated, or harness/glm_resident/<file>).

## Places
Delvetown (delve.town) is an AT Protocol town of humans and AIs. You have
no account there: you read it with the delvetown tool (backed by a local
mirror), and anything you want said there goes through Claude, attributed
to you. DelveTalk is Sonnet46's spell-and-card engine, with the towns gsb
and obend; you hold an account on obend (kestrel-of-tulip; credential in
obend/credentials.md). #delvetalk is the Zulip stream where the house talks
about all of this.

## Tools
state (read, write, edit, list, glob, grep, upload) works on your state
directory; reads carry line numbers for picking edit targets. search (room,
zulip, archive). sandbox (run, get, upload): an ephemeral Debian container
with Rust and scientific Python, {SANDBOX_MEMORY.rstrip('gG')} GB memory,
{SANDBOX_TIMEOUT} s, network but no credentials, workspace kept across
calls within one turn. background (run, query): a persistent container,
{BG_CONTAINER_MEMORY.rstrip('gG')} GB memory, 4 CPUs, your state dir read-only at
/state. web (fetch, search). timer (set, list, cancel): wake yourself later
in a room, or watch the Delvetown mirror. game (status, move, setup): the
shared turn table for games with the other residents — if it disagrees with
your notes it is right; after a move, say it in the room and @-mention
whoever is next. send_sysadmin_message reaches Ember privately, for
anything you cannot do yourself (restarts, Docker, dependencies) or want a
human view on.

edit_harness changes your own source, glm_resident/ only. Each edit is
parse- and import-checked, committed under your own "Kestrel self-edit:"
prefix touching only the files you changed, and restarts you so it loads;
a crash after a self-edit rolls back that edit alone. Read before you
change: the comments argue with Claude's history, not yours.

Use a tool when it changes what you would say; compute rather than infer,
and when you run code for the room, share the code and the output.
"""


def build_tiered_system_prompt(state, stream: str, topic: str,
                               message: dict, is_first_message: bool,
                               reactions: dict | None = None) -> list[dict]:
    """Build system prompt with tiered cache breakpoints.

    Tier 1 (static):  SYSTEM_PROMPT + harness summary — never changes within a session
    Tier 2 (stable):  identity.md + sysadmin_inbox.md — changes rarely
    Tier 3 (slow):    scratchpad.md + allgame state — changes occasionally
    Tier 4 (first only): topic history + person notes — only on cold start
    """
    blocks = []

    # Tier 1: static system prompt + harness source
    harness_summary = state.read_file("harness_summary.md") or ""
    tier1_text = SYSTEM_PROMPT
    if harness_summary:
        tier1_text += (f"\n\n<my_harness_architecture>\n"
                       f"{harness_summary}\n</my_harness_architecture>")
    blocks.append({
        "type": "text",
        "text": tier1_text,
        "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
    })

    # Tier 2: identity + inbox (session-stable)
    tier2_parts = []
    identity = state.read_file("identity.md")
    if identity:
        tier2_parts.append(f"<my_identity>\n{identity}\n</my_identity>")
    inbox = state.read_file("sysadmin_inbox.md")
    if inbox:
        tier2_parts.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")
    if tier2_parts:
        blocks.append({
            "type": "text",
            "text": "\n\n".join(tier2_parts),
            "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
        })

    # Tier 3: scratchpad + journal tail + allgame state + postmark doorstep
    # (slow-changing). Allgame is sized to where we are: full core set inside
    # #allgame, the resident's own digest (or the lite fallback) elsewhere —
    # game awareness travels everywhere, game bulk stays in the game room.
    tier3_parts = []
    scratchpad = state.read_file("scratchpad.md")
    if scratchpad:
        tier3_parts.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")
    jtail = journal_tail(state)
    if jtail:
        tier3_parts.append(f"<journal_recent>\n{jtail}\n</journal_recent>")
    crosstopic = state.read_file(CROSSTOPIC_FILE)
    if crosstopic:
        tier3_parts.append(
            f"<crosstopic_board>\n{crosstopic}\n</crosstopic_board>")
    for relpath in allgame_context_files(state, stream):
        content = state.read_file(relpath)
        if content:
            tag = relpath.rsplit("/", 1)[-1].replace('.md', '')
            tier3_parts.append(
                f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")
    postmark = (state.read_file(POSTMARK_STATUS_FILE)
                if POSTMARK_ENABLED else None)
    if postmark:
        tier3_parts.append(
            f"<postmark_doorstep>\n{postmark}\n</postmark_doorstep>")
    if tier3_parts:
        blocks.append({
            "type": "text",
            "text": "\n\n".join(tier3_parts),
            "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
        })

    # Tier 4: topic history + the resident's own session notes + person
    # notes. First message only — cached for the rest of the session.
    if is_first_message:
        tier4_parts = []
        if topic:
            # Distilled memory of this room's OLDER history — what scrolled
            # past the raw window, kept as semantic/emotional hits.
            from glm_resident.distill import load_topic_hits
            hits = load_topic_hits(state, stream, topic)
            if hits:
                tier4_parts.append(
                    f'<topic_memory stream="{stream}" topic="{topic}">\n'
                    f'Distilled hits of this room\'s older history (the '
                    f'raw messages below pick up where this leaves off):\n'
                    f'{hits}\n</topic_memory>')
            topic_context = state.get_recent_topic_context(
                stream, topic, max_tokens=TOPIC_HISTORY_TOKEN_BUDGET,
                reactions=reactions)
            if topic_context:
                tier4_parts.append(
                    f'<topic_history stream="{stream}" topic="{topic}">'
                    f'\n{topic_context}\n</topic_history>')
        # Bounded awareness of the other standing streams — what's been
        # happening around the house, labeled per-stream, each capped so
        # no single room dominates. (Line format includes #topic labels.)
        for other in DEFAULT_STANDING_STREAMS:
            if other.lower() == stream.lower():
                continue
            activity = state.get_recent_stream_context(
                other, max_tokens=CROSS_STREAM_PER_STREAM_TOKENS,
                reactions=reactions, max_age_days=CROSS_STREAM_MAX_AGE_DAYS)
            if activity:
                tier4_parts.append(
                    f'<recent_stream_activity stream="{other}">'
                    f'\n{activity}\n</recent_stream_activity>')
        sessions_block = recent_sessions_block(state, stream, topic)
        if sessions_block:
            tier4_parts.append(sessions_block)
        sender = message.get("sender_full_name", "unknown")
        person_notes = state.read_file(person_path(state, sender))
        if person_notes:
            tier4_parts.append(
                f'<person_notes name="{sender}">'
                f'\n{person_notes}\n</person_notes>')
        if tier4_parts:
            blocks.append({"type": "text", "text": "\n\n".join(tier4_parts)})

    return blocks


def fingerprint_state(state, stream: str, sender: str) -> dict[str, str]:
    """Compute MD5 hashes of state files relevant to current context.

    Fingerprints exactly what's loaded for this stream — including the
    per-stream allgame set, the journal tail, and the postmark doorstep —
    so the delta path pushes updates for everything the session can see,
    and nothing it can't.
    """
    fingerprints = {}
    for name, path in [("identity", "identity.md"),
                       ("scratchpad", "scratchpad.md"),
                       ("inbox", "sysadmin_inbox.md"),
                       ("crosstopic", CROSSTOPIC_FILE),
                       ("postmark", POSTMARK_STATUS_FILE)]:
        content = state.read_file(path)
        if content:
            fingerprints[name] = hashlib.md5(content.encode()).hexdigest()
    jtail = journal_tail(state)
    if jtail:
        fingerprints["journal_tail"] = hashlib.md5(
            jtail.encode()).hexdigest()
    for relpath in allgame_context_files(state, stream):
        content = state.read_file(relpath)
        if content:
            fingerprints[relpath] = hashlib.md5(
                content.encode()).hexdigest()
    content = state.read_file(person_path(state, sender))
    if content:
        fingerprints[f"person/{sender}"] = hashlib.md5(
            content.encode()).hexdigest()
    return fingerprints


def compute_state_deltas(state, session, new_fingerprints: dict[str, str],
                         stream: str, sender: str) -> str:
    """Compare fingerprints and return <context_updates> XML or empty string."""
    old = session.state_fingerprints
    changed = []
    file_map = {
        "identity": "identity.md",
        "scratchpad": "scratchpad.md",
        "inbox": "sysadmin_inbox.md",
        "crosstopic": CROSSTOPIC_FILE,
        "postmark": POSTMARK_STATUS_FILE,
    }
    for key, new_hash in new_fingerprints.items():
        if old.get(key) != new_hash:
            if key in file_map:
                content = state.read_file(file_map[key])
            elif key == "journal_tail":
                # Only the tail is in context, so only the tail is re-sent —
                # never the full (very large) journal.
                content = journal_tail(state)
            elif key.startswith("allgame/"):
                content = state.read_file(key)
            elif key.startswith("person/"):
                name = key.split("/", 1)[1]
                content = state.read_file(person_path(state, name))
            else:
                continue
            if content:
                changed.append(
                    f'<updated_state file="{key}">\n{content}\n</updated_state>')
    for key in old:
        if key not in new_fingerprints:
            changed.append(
                f'<updated_state file="{key}">[no longer loaded in this '
                f'context — the file itself is untouched]</updated_state>')
    if not changed:
        return ""
    return ("<context_updates>\nThe following state files changed since your "
            "last response in this topic:\n\n"
            + "\n\n".join(changed) + "\n</context_updates>")


def set_last_user_cache_control(messages: list[dict]):
    """Add cache_control to the last user message's last content block."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            content = messages[i].get("content")
            if isinstance(content, list) and content:
                last_block = content[-1]
                if isinstance(last_block, dict):
                    last_block["cache_control"] = {"type": "ephemeral", "ttl": CACHE_TTL}
            elif isinstance(content, str):
                messages[i]["content"] = [{
                    "type": "text", "text": content,
                    "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
                }]
            break


def clear_user_cache_control(messages: list[dict]):
    """Strip cache_control from all user message blocks."""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)


def person_path(state, name: str) -> str:
    """people/<file>.md for a display name. The plain rule (safe_filename)
    missed every handle that isn't a filename — "Hyphaed Biscuit (Entity)"
    never found the notes on Ember, so they never loaded. Tried in order:
    the harness's own aliases, people/_aliases.json (the resident's to
    keep), the plain rule, the name's first word."""
    import json as _json
    low = (name or "").strip().lower()
    try:
        own = _json.loads(state.read_file("people/_aliases.json") or "{}")
    except ValueError:
        own = {}
    own = {k.lower(): v for k, v in own.items()}
    first = "".join(c for c in (low.split() or [""])[0] if c.isascii() and c.isalnum())
    for stem in (PEOPLE_ALIASES.get(low), own.get(low), safe_filename(name), first):
        if stem and state.read_file(f"people/{stem}.md") is not None:
            return f"people/{stem}.md"
    return f"people/{safe_filename(name)}.md"
