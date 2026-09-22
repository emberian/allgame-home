"""DiscordBackend — ChatBackend over discord.py.

## The threading problem, and the shape it forces

discord.py is asyncio: the gateway is a websocket with a heartbeat, driven
by an event loop. This harness is synchronous and threaded — a single turn
takes 20-150 seconds of blocking work (the Agent SDK spawns a subprocess and
waits on it), and the tools call `chat.send_message(...)` straight from
inside that blocking call.

Those two cannot share a thread. If `on_message` awaited a turn, the
heartbeat would stop, Discord would declare the connection dead, and the
gateway would reconnect mid-sentence — every time he said anything
interesting enough to take a while.

So the client owns a dedicated thread with its own event loop, and this
class is the sync façade over it: every public method here hands a coroutine
to that loop with `run_coroutine_threadsafe` and blocks on the result.
`discord_loop` runs the resident's turns on yet another thread, so the
gateway keeps beating while he thinks.

## Why reactions need a cache

Zulip's `add_reaction` takes just a message id. Discord's API cannot fetch a
message without knowing its channel, and the harness's reaction tool only
ever carries an id (the wire format is Zulip-shaped — see backend.py). So
every message this backend normalizes records its channel in `_channel_of`,
and reactions look it up there. The cache is bounded and lossy by design: a
reaction to something far enough back to have been evicted fails with a
clear message rather than growing the map forever.

## Identity

Discord has no email. `sender_email` is synthesized as
`<user_id>@discord.local` — the numeric id, not the handle, because handles
change and the id never does. `people/*.md` notes and DM routing key off
this string, so changing the scheme later would orphan his notes on
everyone he has met.
"""

import asyncio
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from glm_resident.config import DISCORD_STREAM_PREFIX

logger = logging.getLogger("discord_backend")

# Discord's hard per-message ceiling. Not advisory: the API rejects 2001
# characters outright.
DISCORD_MAX_MESSAGE_CHARS = 2000

# How many message-id -> channel-id pairs to remember, for reactions.
CHANNEL_CACHE_SIZE = 5_000

# Hosts whose links are only readable with the bot's own session.
_DISCORD_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net",
                      "images-ext-1.discordapp.net",
                      "images-ext-2.discordapp.net"}

# A synthetic-email domain that cannot collide with a real one.
EMAIL_DOMAIN = "discord.local"


def synth_email(user_id: int) -> str:
    """The stable identity string for a Discord user. See module docstring —
    keyed on the numeric id because handles are mutable and notes are not."""
    return f"{user_id}@{EMAIL_DOMAIN}"


class DiscordBackend:
    name = "discord"
    max_message_chars = DISCORD_MAX_MESSAGE_CHARS

    def __init__(self, token: str, guild_id: int | None = None,
                 call_timeout: float = 30.0):
        import discord

        self.token = token
        self.guild_id = guild_id
        self.call_timeout = call_timeout

        intents = discord.Intents.default()
        # message_content is a PRIVILEGED intent: without it every
        # message arrives with content="" and the resident sits in a room
        # that looks permanently silent. It must also be enabled on the
        # application's Bot page in the Discord developer portal — the
        # library cannot turn it on for you, and the failure is invisible.
        intents.message_content = True
        intents.members = True          # also privileged; needed by get_members
        intents.reactions = True

        self.client = discord.Client(intents=intents)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._channel_of: OrderedDict[int, int] = OrderedDict()
        self._profile: dict = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self, on_ready=None) -> None:
        """Start the gateway on its own thread and block until connected."""
        import discord

        @self.client.event
        async def on_ready():  # noqa: F811 — discord.py dispatches by name
            self._profile = {
                "full_name": self.client.user.display_name,
                "email": synth_email(self.client.user.id),
                "user_id": self.client.user.id,
                "is_bot": True,
            }
            logger.info(
                f"Discord connected as {self.client.user} "
                f"(id={self.client.user.id}), "
                f"{len(self.client.guilds)} guild(s)")
            self._ready.set()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self.client.start(self.token))
            except Exception as e:
                logger.error(f"Discord gateway stopped: {e}", exc_info=True)
            finally:
                self._ready.set()   # never leave start() hanging on failure

        self._thread = threading.Thread(target=_run, name="discord-gateway",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=60):
            raise RuntimeError("Discord gateway did not connect within 60s")
        if not self.client.user:
            raise RuntimeError(
                "Discord gateway failed to log in — check the token and that "
                "the message_content intent is enabled in the developer portal")

    def stop(self) -> None:
        if self._loop and self.client:
            asyncio.run_coroutine_threadsafe(self.client.close(), self._loop)

    def _call(self, coro):
        """Run a coroutine on the gateway loop and wait for it."""
        if self._loop is None:
            raise RuntimeError("Discord backend not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=self.call_timeout)

    # ------------------------------------------------------------------
    # message-id -> channel bookkeeping (see module docstring)
    # ------------------------------------------------------------------
    def remember(self, message_id: int, channel_id: int) -> None:
        self._channel_of[message_id] = channel_id
        self._channel_of.move_to_end(message_id)
        while len(self._channel_of) > CHANNEL_CACHE_SIZE:
            self._channel_of.popitem(last=False)

    # ------------------------------------------------------------------
    # normalization: discord.Message -> the harness's message dict
    # ------------------------------------------------------------------
    @staticmethod
    def _rewrite_mentions(message) -> str:
        """Discord's raw `<@1234>` mention markup -> Zulip's `@**Name**`.

        Not cosmetic. The judge decides a message is DIRECTED at him by
        looking for `@**{bot_name}**` / `@{bot_name}` in the text
        (judge.py). On the wire Discord sends a numeric id, so without this
        every @-mention of Kestrel in Discord would read as ordinary
        ambient chatter — he would be gated by the lurk-decay threshold
        exactly when someone was calling him by name, which from inside
        the room looks like being ignored on purpose.

        Rewriting here rather than teaching the judge two syntaxes keeps
        one mention convention in the harness and puts the per-platform
        translation in the per-platform module.
        """
        content = message.content or ""
        for user in message.mentions:
            for form in (f"<@{user.id}>", f"<@!{user.id}>"):
                content = content.replace(form, f"@**{user.display_name}**")
        for role in getattr(message, "role_mentions", []):
            content = content.replace(f"<@&{role.id}>", f"@**{role.name}**")
        for ch in getattr(message, "channel_mentions", []):
            content = content.replace(f"<#{ch.id}>", f"#**{ch.name}**")
        return content

    def normalize(self, message) -> dict:
        """One discord.Message as the dict the resident consumes.

        See backend.py for the contract and the channel/topic mapping. In
        short: a thread is a topic inside its parent channel; a plain
        channel is a stream whose only topic is named after itself.
        """
        import discord

        ch = message.channel
        is_dm = isinstance(ch, (discord.DMChannel, discord.GroupChannel))

        if is_dm:
            # DMs already route by sender_email, which is platform-unique.
            stream, topic = "dm", message.author.display_name
        elif isinstance(ch, discord.Thread):
            parent = ch.parent
            stream = self.stream_name(parent.name if parent else "unknown")
            topic = ch.name
        else:
            # A channel with no threads reads as a one-topic stream. The
            # redundancy buys zero changes in state/prompt/distill/session.
            stream = self.stream_name(getattr(ch, "name", "unknown"))
            topic = getattr(ch, "name", "unknown")

        self.remember(message.id, ch.id)

        content = self._rewrite_mentions(message)
        # Attachments are links in the text so fetch_url can pick them up,
        # the same way a Zulip upload arrives as a /user_uploads/ link.
        for att in message.attachments:
            content += f"\n[{att.filename}]({att.url})"

        reactions = []
        for r in message.reactions:
            emoji = getattr(r.emoji, "name", None) or str(r.emoji)
            reactions.append({"emoji_name": emoji, "count": r.count})

        return {
            "id": message.id,
            "type": "private" if is_dm else "stream",
            "content": content,
            "sender_full_name": message.author.display_name,
            "sender_email": synth_email(message.author.id),
            "display_recipient": stream,
            "subject": topic,
            "timestamp": int(message.created_at.timestamp()),
            "reactions": reactions,
            # Kept for the Discord loop's own use; harmless downstream.
            "_discord_channel_id": ch.id,
            "_discord_is_dm": is_dm,
        }

    # ------------------------------------------------------------------
    # channel resolution
    # ------------------------------------------------------------------
    @staticmethod
    def stream_name(channel_name: str) -> str:
        """Discord channel -> the harness's stream name (prefixed)."""
        return f"{DISCORD_STREAM_PREFIX}{channel_name}"

    @staticmethod
    def channel_name(stream: str) -> str:
        """The inverse: strip the prefix to get back the Discord channel.

        Tolerates an unprefixed name so a hand-written send_message(to=...)
        from the resident still lands instead of silently failing.
        """
        if stream.startswith(DISCORD_STREAM_PREFIX):
            return stream[len(DISCORD_STREAM_PREFIX):]
        return stream

    def _guild(self):
        if self.guild_id:
            return self.client.get_guild(self.guild_id)
        guilds = self.client.guilds
        return guilds[0] if guilds else None

    def _resolve_channel(self, stream: str, topic: str | None):
        """(stream, topic) -> a sendable discord channel or thread.

        Mirrors `normalize`: topic == stream means the channel itself;
        anything else is a thread by that name inside it.
        """
        import discord

        guild = self._guild()
        if guild is None:
            return None
        channel = discord.utils.get(guild.text_channels,
                                    name=self.channel_name(stream))
        if channel is None:
            return None
        if not topic or topic == stream:
            return channel
        thread = discord.utils.get(channel.threads, name=topic)
        return thread or channel

    # ------------------------------------------------------------------
    # ChatBackend surface
    # ------------------------------------------------------------------
    def get_profile(self) -> dict:
        return dict(self._profile) if self._profile else {"result": "error"}

    def get_members(self) -> dict:
        guild = self._guild()
        if guild is None:
            return {"result": "error", "members": []}
        return {"result": "success", "members": [
            {"user_id": m.id, "full_name": m.display_name,
             "email": synth_email(m.id), "is_bot": m.bot}
            for m in guild.members]}

    def get_streams(self) -> dict:
        guild = self._guild()
        if guild is None:
            return {"result": "error", "streams": []}
        return {"result": "success", "streams": [
            {"name": c.name, "stream_id": c.id}
            for c in guild.text_channels]}

    def send_message(self, payload: dict) -> dict:
        content = payload.get("content", "")
        if payload.get("type") == "private":
            targets = payload.get("to") or []
            uid = targets[0] if isinstance(targets, list) else targets
            return self._call(self._send_dm(int(uid), content))
        return self._call(self._send_channel(
            payload.get("to", ""), payload.get("topic"), content))

    async def _send_channel(self, stream: str, topic: str | None,
                            content: str) -> dict:
        target = self._resolve_channel(stream, topic)
        if target is None:
            return {"result": "error",
                    "msg": f"No Discord channel '{stream}' (topic {topic!r})"}
        msg = await target.send(content)
        self.remember(msg.id, target.id)
        return {"result": "success", "id": msg.id}

    async def _send_dm(self, user_id: int, content: str) -> dict:
        user = self.client.get_user(user_id)
        if user is None:
            try:
                user = await self.client.fetch_user(user_id)
            except Exception as e:
                return {"result": "error", "msg": f"No such user {user_id}: {e}"}
        msg = await user.send(content)
        self.remember(msg.id, msg.channel.id)
        return {"result": "success", "id": msg.id}

    def add_reaction(self, payload: dict) -> dict:
        mid = payload.get("message_id")
        emoji = payload.get("emoji_name", "")
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            return {"result": "error", "msg": f"Bad message_id {mid!r}"}
        chan_id = self._channel_of.get(mid)
        if chan_id is None:
            return {"result": "error",
                    "msg": (f"Message {mid} is not in the recent-channel "
                            f"cache, so its channel is unknown — can only "
                            f"react to recent messages on Discord.")}
        return self._call(self._add_reaction(chan_id, mid, emoji))

    async def _add_reaction(self, chan_id: int, mid: int, emoji: str) -> dict:
        channel = self.client.get_channel(chan_id)
        if channel is None:
            return {"result": "error", "msg": f"No channel {chan_id}"}
        try:
            msg = await channel.fetch_message(mid)
            # Zulip emoji names ("laughing") are not Discord emoji. Unicode
            # passes through; a bare name is wrapped as :name: and left to
            # the guild's custom emoji, failing clearly if absent.
            await msg.add_reaction(emoji if not emoji.isascii()
                                   else f":{emoji}:" if emoji.isalpha()
                                   else emoji)
            return {"result": "success"}
        except Exception as e:
            return {"result": "error", "msg": f"add_reaction failed: {e}"}

    def set_typing_status(self, payload: dict) -> dict:
        # Discord typing indicators expire after ~10s and cannot be
        # cancelled, so "stop" is a no-op rather than a lie.
        if payload.get("op") == "stop":
            return {"result": "success"}
        target = self._resolve_channel(payload.get("to", ""),
                                       payload.get("topic"))
        if target is None:
            return {"result": "error"}
        try:
            self._call(target.typing().__aenter__())
        except Exception:
            pass   # cosmetic; never fail a turn over it
        return {"result": "success"}

    def get_messages(self, request: dict) -> dict:
        """History, from the Zulip-shaped narrow vocabulary in use.

        Understood operators: `channel`, `topic`, `is`=private. That is the
        whole set the harness passes (see backend.ChatBackend.get_messages).
        `search` is NOT supported — Discord's API has no message search for
        bots, so search_zulip_history degrades rather than lying.
        """
        narrow = request.get("narrow") or []
        if isinstance(narrow, str):
            import json
            narrow = json.loads(narrow)
        terms = {n.get("operator"): n.get("operand")
                 for n in narrow if isinstance(n, dict)}
        if "search" in terms:
            return {"result": "error",
                    "msg": "Discord has no bot-accessible message search."}
        limit = int(request.get("num_before", 50)) or 50
        if terms.get("is") == "private":
            return self._call(self._dm_history(limit))
        return self._call(self._channel_history(
            terms.get("channel"), terms.get("topic"), limit))

    async def _channel_history(self, stream, topic, limit) -> dict:
        targets = []
        guild = self._guild()
        if guild is None:
            return {"result": "error", "messages": []}
        if stream:
            target = self._resolve_channel(stream, topic)
            if target is None:
                return {"result": "error", "messages": [],
                        "msg": f"No channel '{stream}'"}
            targets = [target]
        else:
            targets = list(guild.text_channels)
        out = []
        for t in targets:
            try:
                async for m in t.history(limit=limit):
                    out.append(self.normalize(m))
            except Exception as e:
                logger.debug(f"history({t}) failed: {e}")
        out.sort(key=lambda m: m["id"])
        return {"result": "success", "messages": out}

    async def _dm_history(self, limit) -> dict:
        out = []
        for ch in self.client.private_channels:
            try:
                async for m in ch.history(limit=limit):
                    out.append(self.normalize(m))
            except Exception as e:
                logger.debug(f"dm history({ch}) failed: {e}")
        out.sort(key=lambda m: m["id"])
        return {"result": "success", "messages": out}

    # ------------------------------------------------------------------
    # files / urls
    # ------------------------------------------------------------------
    def upload_file(self, filepath: Path) -> dict:
        """Discord has no upload-then-reference step: a file is attached to
        a message. So there is no URI to hand back for pasting, and the
        upload tools cannot work the way they do on Zulip.

        Returning an error beats inventing a URL the resident would then
        paste into a room as a dead link.
        """
        return {"result": "error",
                "msg": ("Discord attaches files to messages rather than "
                        "hosting them separately, so there is no link to "
                        "paste. Post the file's contents in a message, or "
                        "share it on Zulip.")}

    def claim_url(self, url: str) -> str | None:
        if url.startswith("/"):
            return None      # Discord has no relative attachment paths
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return None
        return url if host in _DISCORD_CDN_HOSTS else None

    def fetch_authed(self, url: str, timeout: int = 30):
        # Discord CDN links carry their own signed query parameters and are
        # readable without the bot token; a plain GET is the authed path.
        import requests
        return requests.get(url, timeout=timeout, headers={
            "User-Agent": "Kestrel-Resident/1.0", "Accept": "*/*"})
