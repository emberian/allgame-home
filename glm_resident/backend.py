"""ChatBackend — the chat platform the resident lives on, behind one seam.

Kestrel is meant to be a member of two places at once: Tulip (a Zulip fork)
and a Discord server. Not a bot mirrored into a second room by a relay, but
the same resident, with one memory, answering in both.

## Where the seam is, and where it deliberately is not

The seam is OUTBOUND operations plus history reads. It is NOT the event
loop. Zulip's event model (register a queue, poll `get_events` with a
`last_event_id`, handle `BAD_EVENT_QUEUE_ID`) and Discord's (a persistent
websocket gateway dispatching callbacks) have nothing in common worth
unifying — an abstraction over both would be a third thing, more complex
than either, that neither platform's failure modes fit inside. So each
platform keeps its own loop (`zulip_loop.py`, `discord_loop.py`), and each
loop's job is to normalize what it hears into the message dict below and
hand it to `resident.handle_message`. Everything AFTER that point — the
judge, the prompt, the engine, the tools, the state dir — is already
platform-agnostic and stays untouched.

## The wire format is Zulip-shaped, on purpose

The payloads and method names here are the subset of `zulip.Client` the
harness already used, because that is the format four years of this harness
was written against. Defining a prettier interface would have meant
rewriting every call site — and rewriting working code that a live resident
depends on, to no functional end, is how a refactor earns a regression.

So `ZulipBackend` is a near-passthrough, and `DiscordBackend` translates.
"Zulip-shaped" is an accident of descent, not a claim that Discord is
Zulip-like; read it as "the harness's internal envelope".

## The normalized message (what a loop must produce)

Every key here is consumed somewhere downstream — the counts are how many
call sites read each one, so none of this is aspirational:

    id                  int    platform message id (unique within platform)
    type                str    "stream" for a channel, "private" for a DM
    content             str    the message text
    sender_full_name    str    display name, as the room sees it
    sender_email        str    a STABLE unique id for this person. On
                               Discord there is no email: synthesize one
                               (see DiscordBackend) and never change it —
                               people/*.md notes and DM routing key off it.
    display_recipient   str    channel/stream name (or DM recipient list)
    subject             str    topic. Zulip has these natively; on Discord
                               a thread name is the topic, and a plain
                               channel's topic is the channel's own name.
    timestamp           int    unix seconds
    reactions           list   optional; [{emoji_name, user_id}, ...]

## The channel/topic mapping

`channels/{stream}/{topic}.jsonl`, the distiller, the session keys and the
cross-stream context all address a room as (stream, topic). Discord maps on
cleanly:

    guild            -> not represented; one guild per resident
    text channel     -> stream, and topic = the channel name again
    thread in it     -> stream = parent channel, topic = thread name
    DM               -> type="private", as on Zulip

A channel with no threads therefore looks like a Zulip stream with one
topic named after itself. That is a little redundant and it is the right
trade: it means zero changes to state, prompt, distill or session code.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatBackend(Protocol):
    """Outbound operations + history reads for one chat platform.

    Method names and payload shapes mirror `zulip.Client` (see module
    docstring). Every method returns a dict carrying at least
    `{"result": "success"|"error"}`, because that is what every existing
    call site checks.
    """

    # Platform tag, for logs and for prompt text that must name the room.
    name: str

    # The platform's hard per-message ceiling. This is NOT cosmetic: Zulip
    # takes ~10k characters and Discord rejects anything over 2000, so the
    # chunker in tools/dispatch.py has to ask rather than assume. A resident
    # who assumes Zulip's limit on Discord does not get a truncated
    # message, he gets a failed send and a silent room.
    max_message_chars: int

    def get_profile(self) -> dict:
        """Who the resident is here. Needs `full_name`."""
        ...

    def send_message(self, payload: dict) -> dict:
        """Post one message. Returns {"result", "id"}.

        Payload is one of:
            {"type": "stream",  "to": <stream>, "topic": <topic>,
             "content": <text>}
            {"type": "private", "to": [<recipient ids>], "content": <text>}

        The caller has ALREADY split `content` to `max_message_chars`.
        """
        ...

    def add_reaction(self, payload: dict) -> dict:
        """React to a message: {"message_id", "emoji_name"}.

        Note for implementors: Zulip needs only the message id, while
        Discord needs the channel too in order to fetch the message. A
        Discord backend therefore has to remember which channel a message
        id came from — see DiscordBackend._channel_of.
        """
        ...

    def set_typing_status(self, payload: dict) -> dict:
        """Show/hide a typing indicator. Best-effort; never load-bearing."""
        ...

    def get_members(self) -> dict:
        """Realm/guild members, as {"members": [{user_id, full_name, ...}]}.
        Used to resolve a DM recipient typed as a display name."""
        ...

    def get_streams(self) -> dict:
        """Visible channels, as {"streams": [{name, stream_id}]}."""
        ...

    def get_messages(self, request: dict) -> dict:
        """Read history. Returns {"result", "messages": [<normalized>]}.

        Request keys the harness actually passes: `anchor` ("newest"),
        `num_before`, `num_after`, `narrow`, `apply_markdown`. The narrow is
        a list of {"operator", "operand"} with operators `channel`,
        `topic` and `is` ("private") — that is the whole vocabulary in use,
        so a non-Zulip backend only has to understand those three.
        """
        ...

    def upload_file(self, filepath: Path) -> dict:
        """Upload an attachment. Returns {"result", "uri"/"url", "filename"}.
        The uri is what gets pasted into a message to share the file."""
        ...

    def claim_url(self, url: str) -> str | None:
        """If `url` belongs to this platform, return it absolute; else None.

        This is how `fetch_url` knows to spend the bot's own credentials on
        a link instead of fetching it as an anonymous stranger — Zulip
        `/user_uploads/...` paths and Discord CDN links are both only
        readable that way.
        """
        ...

    def fetch_authed(self, url: str, timeout: int = 30):
        """GET a URL of ours with the bot's session. Returns a
        requests-style response (needs .content, .text, .headers,
        .raise_for_status())."""
        ...
