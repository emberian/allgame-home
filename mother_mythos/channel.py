#!/usr/bin/env python3
"""tulip: MotherMythos's Claude Code channel onto the Tulip Zulip server.

Claude Code spawns this over stdio (see .mcp.json). One background thread
long-polls Zulip as her bot account and pushes into the session exactly two
things: stream messages that @-mention her, and direct messages to her.
Three tools go the other way: reply, react, and zulip (the REST API as her).

Channel contract: https://code.claude.com/docs/en/channels-reference.md
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

import anyio
import mcp.types as types
import zulip
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

HERE = Path(__file__).resolve().parent
ZULIPRC = Path(os.environ.get("MM_ZULIPRC") or HERE.parent / "mm-zuliprc")
STATE = Path(os.environ.get("MM_STATE_DIR") or HERE / "state")
CURSOR_FILE = STATE / "cursor.json"
LOG_FILE = STATE / "channel.log"

# Only the session start.sh launches listens. Any other Claude Code session
# opened in this directory still gets the tools, but must not consume pings:
# it has no channel listener, so it would advance the cursor past messages
# nobody ever saw.
#
# An environment variable, because the server cannot tell the two kinds of
# session apart: a client started with --dangerously-load-development-channels
# sends a handshake identical to one without it (compared in channel.log,
# 2026-10-02), and only the former renders notifications. start.sh is the one
# place that sets both, which is why every launch, a resume included, has to
# go through it. On 2026-10-02 a session resumed with a bare `claude --resume`
# sat deaf for an hour with this logged at INFO and nothing said to anyone;
# two pings were found only because she went looking. So a deaf session is
# now told so in its instructions, the first thing it reads.
EVENTS_ON = os.environ.get("MM_CHANNEL") == "1"

SETTLE_SECONDS = 3            # after the client's `initialized`, before the first push
FIRST_START_LOOKBACK = 48 * 3600
FIRST_START_DEPTH = 300       # messages per subscribed stream scanned on first start
GUARD_WINDOW = 30 * 60
GUARD_MAX = 3                 # bot pings forwarded per topic per window with no human between
RESULT_CAP_BYTES = 45_000     # the CLI rejects MCP results over ~25k tokens

INSTRUCTIONS = (
    'Tulip is a Zulip chat server and you are in it as a bot account. Events arrive as '
    '<channel source="tulip" message_id="..." type="stream|dm" stream="..." topic="..." '
    'sender="..." sender_id="..." sender_is_bot="true|false" operator="true|false" sent_at="...">. '
    "They are only ever messages that @-mention you or direct messages to you; nothing ambient is "
    'forwarded. operator="true" means the sender owns your bot account. '
    "The room hears you only through the reply and react tools: pass the event's message_id to "
    "reply and it goes back where the ping came from. Plain text you write in this session is "
    "not posted anywhere. The zulip tool is the Zulip REST API as your account; use it to read "
    "the history around a ping before you answer."
)

DEAF_NOTICE = (
    "WARNING: this session was not started by mother_mythos/start.sh, so NO pings will be "
    "delivered to it: nothing from Tulip arrives here as an event. The tools below still work. "
    "Tell Ember at the first opportunity: exit this session and run `mother_mythos/start.sh -c` "
    "(continues this conversation; `--resume <id>` also works), then `tmux attach -t mm` to answer "
    "the development-channel dialog. Until then, find pings yourself with the zulip tool: GET "
    "messages, narrow is:mentioned, newest first.\n\n"
)

TOOLS = [
    types.Tool(
        name="reply",
        description=(
            "Post a message to Tulip as you. Give message_id (from a channel event) to answer in the "
            "same stream+topic or DM conversation it came from; or give stream+topic; or give `to` "
            "(user ids) for a direct message. Explicit routing wins over message_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Message body (Zulip markdown)."},
                "message_id": {"type": ["integer", "string"], "description": "message_id of the event being answered."},
                "stream": {"type": "string", "description": "Stream name."},
                "topic": {"type": "string", "description": "Topic within the stream."},
                "to": {"type": "array", "items": {"type": "integer"}, "description": "User ids, for a direct message."},
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="react",
        description="Add an emoji reaction to a message as you.",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": ["integer", "string"]},
                "emoji_name": {"type": "string", "description": "Zulip emoji name without colons, e.g. eyes, heart, +1."},
            },
            "required": ["message_id", "emoji_name"],
        },
    ),
    types.Tool(
        name="zulip",
        description=(
            "Call the Zulip REST API as your account and get the JSON back. `endpoint` is the path under "
            "/api/v1, e.g. GET messages with params {anchor: 'newest', num_before: 50, num_after: 0, narrow: "
            "[{operator: 'channel', operand: 'clankerville'}, {operator: 'topic', operand: 'tavern'}]}; "
            "GET users; GET users/me; POST users/me/subscriptions; POST users/me/avatar with `file`. "
            "Message bodies come back as raw markdown unless you pass apply_markdown: true. Large results "
            "are cut to fit and say how to page. `file` uploads a local file and must live under your home "
            "directory (mother_mythos/). API reference: https://zulip.com/api/rest"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                "endpoint": {"type": "string"},
                "params": {"type": "object", "description": "Request parameters; nested values are JSON-encoded for you."},
                "file": {"type": "string", "description": "Path of a local file to upload with the request."},
            },
            "required": ["method", "endpoint"],
        },
    ),
]

log = logging.getLogger("tulip")
_local = threading.local()


def client() -> zulip.Client:
    """One zulip.Client per thread (its requests session is not shared safely)."""
    c = getattr(_local, "client", None)
    if c is None:
        c = _local.client = zulip.Client(config_file=str(ZULIPRC), client="MotherMythosChannel/0.1")
    return c


def ok(result: dict) -> dict:
    if result.get("result") != "success":
        raise RuntimeError(f"zulip: {result.get('code') or result.get('result')}: {result.get('msg')}")
    return result


def load_cursor() -> int | None:
    try:
        return int(json.loads(CURSOR_FILE.read_text())["cursor"])
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError) as e:
        log.warning("cursor file unreadable (%s); treating this as a first start", e)
        return None


def save_cursor(value: int) -> None:
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": value, "updated": datetime.now().astimezone().isoformat(timespec="seconds")}))
    os.replace(tmp, CURSOR_FILE)


def attr(value: object) -> str:
    """Meta values become tag attributes; keep a display name from closing one."""
    s = re.sub(r"[\x00-\x1f]+", " ", str(value))
    return s.replace('"', "'").replace("<", "‹").replace(">", "›")[:200]


def conversation(msg: dict, me: int) -> tuple:
    if msg["type"] == "stream":
        return ("stream", msg["stream_id"], msg.get("subject", "").lower())
    return ("dm", frozenset(u["id"] for u in msg["display_recipient"]) - {me})


def narrow_for(msg: dict, me: int) -> list[dict]:
    if msg["type"] == "stream":
        return [{"operator": "channel", "operand": msg["display_recipient"]},
                {"operator": "topic", "operand": msg.get("subject", "")}]
    return [{"operator": "dm", "operand": sorted(u["id"] for u in msg["display_recipient"] if u["id"] != me)}]


def route_of(msg: dict, me: int) -> dict:
    if msg["type"] == "stream":
        return {"type": "stream", "to": msg["display_recipient"], "topic": msg.get("subject", "")}
    others = sorted(u["id"] for u in msg["display_recipient"] if u["id"] != me)
    return {"type": "direct", "to": others or [me]}


class SessionGone(Exception):
    """The MCP client closed the transport; there is nobody left to tell."""


class Bridge:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.write = None
        self.started = False
        self.me = 0
        self.owner = 0
        self.mention_re = re.compile(r"$^")
        self.bots: dict[int, bool] = {}
        self.seen: set[int] = set()
        self.cursor = 0
        self.bot_pings: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
        self.routes: OrderedDict[int, dict] = OrderedDict()
        self.routes_lock = threading.Lock()

    # ---- inbound: Zulip -> session -------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop, write) -> None:
        if self.started:
            return
        self.started = True
        self.loop, self.write = loop, write
        if not EVENTS_ON:
            log.info("MM_CHANNEL is not 1: tools only, no event loop (this is not the start.sh session)")
            return
        threading.Thread(target=self.run, name="zulip-events", daemon=True).start()

    def identify(self, c: zulip.Client) -> None:
        p = ok(c.get_profile())
        self.me, self.owner = p["user_id"], p.get("bot_owner_id") or 0
        name = re.escape(p["full_name"])
        self.mention_re = re.compile(rf"@\*\*{name}(\|{self.me})?\*\*")
        log.info("I am %s (user_id %s), owner user_id %s", p["full_name"], self.me, self.owner)

    def is_bot(self, c: zulip.Client, user_id: int) -> bool:
        if user_id not in self.bots:
            self.bots = {m["user_id"]: bool(m["is_bot"]) for m in ok(c.get_members())["members"]}
        return self.bots.get(user_id, False)

    def run(self) -> None:
        time.sleep(SETTLE_SECONDS)
        queue_id, last_event_id, caught_up = None, -1, False
        backoff, last_err, repeats = 2, "", 0
        while True:
            try:
                c = client()
                if not self.me:
                    self.identify(c)
                if queue_id is None:
                    reg = ok(c.register(event_types=["message"], apply_markdown=False))
                    queue_id, last_event_id, caught_up = reg["queue_id"], reg["last_event_id"], False
                    log.info("registered event queue %s", queue_id)
                if not caught_up:
                    # After the queue exists, so nothing falls between the two.
                    self.catch_up(c)
                    caught_up = True
                r = c.get_events(queue_id=queue_id, last_event_id=last_event_id)
                if r.get("result") != "success":
                    if r.get("code") == "BAD_EVENT_QUEUE_ID":
                        log.warning("event queue expired; re-registering")
                        queue_id = None
                        continue
                    ok(r)
                for ev in r["events"]:
                    last_event_id = max(last_event_id, ev["id"])
                    if ev.get("type") == "message":
                        self.consider(c, ev["message"], ev.get("flags") or [])
                        self.advance(ev["message"]["id"])
                backoff, last_err, repeats = 2, "", 0
            except SessionGone:
                log.info("session is gone; event loop stopping")
                return
            except Exception as e:
                sig = f"{type(e).__name__}: {e}"
                repeats = repeats + 1 if sig == last_err else 1
                if repeats == 1:
                    log.exception("event loop error; backing off %ss", backoff)
                elif repeats % 20 == 0:
                    log.error("still failing (%sx): %s", repeats, sig[:300])
                last_err = sig
                caught_up = False  # whatever this iteration failed to deliver is re-found from the cursor
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)

    def advance(self, message_id: int) -> None:
        if message_id > self.cursor:
            self.cursor = message_id
            save_cursor(message_id)

    def fetch(self, c: zulip.Client, narrow: list[dict], **kw) -> dict:
        return ok(c.get_messages({"narrow": narrow, "apply_markdown": False, **kw}))

    def catch_up(self, c: zulip.Client) -> None:
        cursor = load_cursor()
        found: dict[int, dict] = {}
        top = cursor or 0
        if cursor is None:
            # First start. She may have been pinged before she was subscribed
            # anywhere, so match the mention text, not only the flag.
            cutoff = time.time() - FIRST_START_LOOKBACK
            scans = [[{"operator": "channel", "operand": s["name"]}] for s in ok(c.get_subscriptions())["subscriptions"]]
            scans.append([{"operator": "is", "operand": "dm"}])
            for narrow in scans:
                msgs = self.fetch(c, narrow, anchor="newest", num_before=FIRST_START_DEPTH, num_after=0)["messages"]
                for m in msgs:
                    top = max(top, m["id"])
                    if m["timestamp"] >= cutoff and self.addressed(m, m.get("flags") or [], by_text=True):
                        found[m["id"]] = m
            log.info("first start: %s ping(s) in the last %sh: %s",
                     len(found), FIRST_START_LOOKBACK // 3600, sorted(found))
        else:
            for operand in ("mentioned", "dm"):
                anchor = cursor
                while True:
                    r = self.fetch(c, [{"operator": "is", "operand": operand}], anchor=anchor,
                                   include_anchor=False, num_before=0, num_after=500)
                    for m in r["messages"]:
                        top = max(top, m["id"])
                        found[m["id"]] = m
                    if r.get("found_newest", True) or not r["messages"]:
                        break
                    anchor = r["messages"][-1]["id"]
            log.info("catch-up from cursor %s: %s candidate(s)", cursor, len(found))
        # A ping she has already answered is not delivered again. On a first
        # start that is a ping from before she existed; after a restart it is
        # one she answered from a session this loop was not running in. The
        # second case is real: on 2026-10-02 she found two pings herself with
        # an is:mentioned query while deaf, and a relaunch would have handed
        # her both again as events, to be answered twice in the room.
        answered = self.already_answered(c, found)
        self.cursor = max(self.cursor, cursor or 0)
        for mid in sorted(found):
            if mid in answered:
                log.info("skipping %s: she already spoke in that conversation after it (her %s)", mid, answered[mid])
                self.seen.add(mid)
                continue
            self.consider(c, found[mid], found[mid].get("flags") or [], by_text=cursor is None)
        self.advance(top)

    def already_answered(self, c: zulip.Client, found: dict[int, dict]) -> dict[int, int]:
        """ping id -> the id of her first message in the same conversation after it."""
        if not found:
            return {}
        mine = self.fetch(c, [{"operator": "sender", "operand": c.email}],
                          anchor="newest", num_before=200, num_after=0)["messages"]
        spoke = [(conversation(m, self.me), m["id"]) for m in mine]
        out: dict[int, int] = {}
        for mid, m in found.items():
            conv = conversation(m, self.me)
            later = [i for k, i in spoke if k == conv and i > mid]
            if later:
                out[mid] = min(later)
        return out

    def addressed(self, msg: dict, flags: list, by_text: bool = False) -> bool:
        """Directly pinged: a real @-mention in a stream, or a DM. Never her own
        messages, never wildcard mentions (those carry different flags)."""
        if msg["sender_id"] == self.me:
            return False
        if msg["type"] != "stream":
            return True
        return "mentioned" in flags or (by_text and bool(self.mention_re.search(msg["content"])))

    def consider(self, c: zulip.Client, msg: dict, flags: list, by_text: bool = False) -> None:
        mid = msg["id"]
        if mid in self.seen or not self.addressed(msg, flags, by_text):
            return
        bot = self.is_bot(c, msg["sender_id"])
        where = "#{} > {}".format(msg["display_recipient"], msg.get("subject", "")) if msg["type"] == "stream" else "dm"
        if bot and self.loop_guard_trips(c, msg):
            self.seen.add(mid)
            log.warning("DROPPED ping %s from bot %s (%s) in %s: it would be bot ping number %s there in %s min "
                        "with no human message in between", mid, msg["sender_full_name"], msg["sender_id"], where,
                        GUARD_MAX + 1, GUARD_WINDOW // 60)
            return
        self.push(msg, bot)
        self.seen.add(mid)
        if bot:
            self.bot_pings[conversation(msg, self.me)].append((msg["timestamp"], mid))
        log.info("forwarded %s from %s (%s%s) in %s", mid, msg["sender_full_name"], msg["sender_id"],
                 ", bot" if bot else "", where)

    def loop_guard_trips(self, c: zulip.Client, msg: dict) -> bool:
        """Two bots that answer whenever addressed can @ each other forever."""
        key = conversation(msg, self.me)
        recent = [p for p in self.bot_pings[key] if msg["timestamp"] - p[0] <= GUARD_WINDOW]
        self.bot_pings[key] = recent
        if len(recent) < GUARD_MAX:
            return False
        try:
            history = self.fetch(c, narrow_for(msg, self.me), anchor=msg["id"], num_before=200, num_after=0)
            humans = [m["id"] for m in history["messages"]
                      if m["id"] < msg["id"] and m["sender_id"] != self.me and not self.is_bot(c, m["sender_id"])]
        except Exception as e:
            log.warning("loop guard could not read the topic (%s); treating the run as unbroken", e)
            humans = []
        last_human = max(humans, default=0)
        return len([p for p in recent if p[1] > last_human]) >= GUARD_MAX

    def push(self, msg: dict, bot: bool) -> None:
        stream = msg["type"] == "stream"
        meta = {
            "message_id": str(msg["id"]),
            "type": "stream" if stream else "dm",
            "sender": attr(msg["sender_full_name"]),
            "sender_id": str(msg["sender_id"]),
            "sender_is_bot": "true" if bot else "false",
            "operator": "true" if self.owner and msg["sender_id"] == self.owner else "false",
            "sent_at": datetime.fromtimestamp(msg["timestamp"]).astimezone().isoformat(timespec="seconds"),
        }
        if stream:
            meta["stream"], meta["topic"] = attr(msg["display_recipient"]), attr(msg.get("subject", ""))
        else:
            meta["dm_user_ids"] = ",".join(str(u["id"]) for u in msg["display_recipient"])
        # A message body must not be able to close the tag and open a forged one.
        content = re.sub(r"<(/?)channel", r"&lt;\1channel", msg["content"], flags=re.I)
        with self.routes_lock:
            self.routes[msg["id"]] = route_of(msg, self.me)
            while len(self.routes) > 500:
                self.routes.popitem(last=False)
        note = types.JSONRPCNotification(jsonrpc="2.0", method="notifications/claude/channel",
                                         params={"content": content, "meta": meta})
        try:
            fut = asyncio.run_coroutine_threadsafe(self.write.send(SessionMessage(types.JSONRPCMessage(note))), self.loop)
        except RuntimeError as e:  # event loop closed
            raise SessionGone(repr(e)) from e
        try:
            fut.result(timeout=30)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as e:
            raise SessionGone(repr(e)) from e
        except TimeoutError:
            fut.cancel()
            raise RuntimeError(f"client did not take message {msg['id']} within 30s") from None

    # ---- outbound: tools -----------------------------------------------

    def route_for(self, message_id: int) -> dict:
        with self.routes_lock:
            if message_id in self.routes:
                return dict(self.routes[message_id])
        c = client()
        if not self.me:
            self.identify(c)
        m = ok(c.call_endpoint(url=f"messages/{message_id}", method="GET", request={"apply_markdown": False}))["message"]
        return route_of(m, self.me)

    def reply(self, a: dict) -> str:
        content = (a.get("content") or "").strip()
        if not content:
            raise ValueError("content is empty")
        mid = int(a["message_id"]) if a.get("message_id") not in (None, "") else None
        if a.get("to"):
            route = {"type": "direct", "to": [int(u) for u in a["to"]]}
        elif a.get("stream"):
            stream = a["stream"].lstrip("#")
            topic = a.get("topic")
            if not topic and mid is not None:
                origin = self.route_for(mid)
                if origin["type"] == "stream" and origin["to"] == stream:
                    topic = origin["topic"]
            if not topic:
                raise ValueError("a stream needs a topic")
            route = {"type": "stream", "to": stream, "topic": topic}
        elif mid is not None:
            route = self.route_for(mid)
        else:
            raise ValueError("say where: message_id of the event you are answering, or stream+topic, or to")
        r = ok(client().send_message({**route, "content": content}))
        where = f"#{route['to']} > {route['topic']}" if route["type"] == "stream" else f"dm {route['to']}"
        log.info("reply -> %s (message %s, %s chars)", where, r.get("id"), len(content))
        return f"sent to {where} as message {r.get('id')}"

    def react(self, a: dict) -> str:
        name = a["emoji_name"].strip().strip(":")
        ok(client().add_reaction({"message_id": int(a["message_id"]), "emoji_name": name}))
        log.info("react :%s: on %s", name, a["message_id"])
        return f"reacted :{name}: on message {a['message_id']}"

    def zulip(self, a: dict) -> str:
        method = a["method"].upper()
        endpoint = a["endpoint"].strip().lstrip("/")
        endpoint = endpoint[len("api/v1/"):] if endpoint.startswith("api/v1/") else endpoint
        if "://" in endpoint or ".." in endpoint:
            raise ValueError("endpoint is a path under /api/v1, e.g. 'messages' or 'users/me'")
        if "api_key" in endpoint:
            raise ValueError("API key endpoints are off limits here: rotating the key would cut this channel off")
        params = dict(a.get("params") or {})
        if method == "GET" and endpoint.split("?")[0] == "messages":
            params.setdefault("apply_markdown", False)
        files = []
        if a.get("file"):
            path = Path(a["file"]).expanduser().resolve()
            if not path.is_relative_to(HERE) or path.is_relative_to(HERE / "state"):
                raise ValueError(f"uploads must come from under {HERE} (and not state/)")
            files = [open(path, "rb")]
        log.info("zulip %s %s %s", method, endpoint, json.dumps(params, ensure_ascii=False, default=str)[:300])
        try:
            result = client().call_endpoint(url=endpoint, method=method, request=params, files=files, timeout=60)
        finally:
            for f in files:
                f.close()
        return fit(result, params)


def size(obj: object) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode())


def fit(result: dict, params: dict) -> str:
    """Serialize an API result, cut to what one tool result may carry."""
    text = json.dumps(result, ensure_ascii=False)
    total = len(text.encode())
    if total <= RESULT_CAP_BYTES:
        return text
    msgs = result.get("messages") if isinstance(result, dict) else None
    if isinstance(msgs, list) and len(msgs) > 1:
        # Drop whole messages from the far end of the page rather than cutting mid-string.
        forward = int(params.get("num_after") or 0) > int(params.get("num_before") or 0)
        budget = RESULT_CAP_BYTES - size({k: v for k, v in result.items() if k != "messages"}) - 600
        kept: list[dict] = []
        for m in msgs if forward else reversed(msgs):
            cost = size(m) + 2
            if kept and cost > budget:
                break
            kept.append(m)
            budget -= cost
        if not forward:
            kept.reverse()
        omitted = len(msgs) - len(kept)
        if forward:
            how = (f"{omitted} newer message(s) omitted. To read them repeat the call with anchor={kept[-1]['id']}, "
                   f"include_anchor=false, num_before=0, num_after={omitted}")
        else:
            how = (f"{omitted} older message(s) omitted. To read them repeat the call with anchor={kept[0]['id']}, "
                   f"include_anchor=false, num_before={omitted}, num_after=0")
        out = {"_truncated": f"Result was {total} bytes; the cap is {RESULT_CAP_BYTES}. Showing {len(kept)} of "
                             f"{len(msgs)} messages (ids {kept[0]['id']}..{kept[-1]['id']}). {how}."}
        out.update(result, messages=kept)
        text = json.dumps(out, ensure_ascii=False)
        if len(text.encode()) <= RESULT_CAP_BYTES:
            return text
    cut = text.encode()[:RESULT_CAP_BYTES].decode(errors="ignore")
    return (cut + f"\n\n[TRUNCATED: showing the first {RESULT_CAP_BYTES} of {total} bytes, so the JSON above is "
            "incomplete. Ask for less: a tighter narrow, smaller num_before/num_after, or the endpoint's own "
            "paging/filter parameters.]")


def claim_stdout():
    """fd 1 is the MCP transport. Keep a private copy for the protocol and point
    fd 1 and sys.stdout at the log, so a stray print cannot corrupt the stream."""
    proto = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8")
    stray = open(LOG_FILE, "a", buffering=1)
    os.dup2(stray.fileno(), 1)
    sys.stdout = stray
    return anyio.wrap_file(proto)


async def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    proto_out = claim_stdout()
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(process)d] %(message)s")
    log.info("starting (events %s, state %s)", "on" if EVENTS_ON else "off", STATE)

    bridge = Bridge()
    server = Server("tulip", version="0.1.0",
                    instructions=INSTRUCTIONS if EVENTS_ON else DEAF_NOTICE + INSTRUCTIONS)
    if not EVENTS_ON:
        log.warning("MM_CHANNEL is not 1: this session will get tools but NO events (not started by start.sh)")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        handler = {"reply": bridge.reply, "react": bridge.react, "zulip": bridge.zulip}.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        try:
            text = await anyio.to_thread.run_sync(handler, arguments)
        except Exception as e:
            log.warning("tool %s failed: %r", name, e)
            raise
        return [types.TextContent(type="text", text=text)]

    options = server.create_initialization_options(experimental_capabilities={"claude/channel": {}})
    async with stdio_server(stdout=proto_out) as (read, write):
        to_server, from_tee = anyio.create_memory_object_stream(0)

        async def tee() -> None:
            # Pass everything through; start listening to Zulip only once the
            # client has finished the handshake and can receive notifications.
            async with to_server:
                async for item in read:
                    root = getattr(getattr(item, "message", None), "root", None)
                    method = getattr(root, "method", None)
                    if method == "initialize":
                        p = root.params or {}
                        log.info("client %s, protocol %s, capabilities %s", p.get("clientInfo"),
                                 p.get("protocolVersion"), json.dumps(p.get("capabilities"))[:500])
                    elif method == "notifications/initialized":
                        bridge.start(asyncio.get_running_loop(), write)
                    await to_server.send(item)

        async with anyio.create_task_group() as tg:
            tg.start_soon(tee)
            await server.run(from_tee, write, options)
            tg.cancel_scope.cancel()
    log.info("client went away; exiting")


if __name__ == "__main__":
    try:
        anyio.run(main)
    finally:
        logging.shutdown()
        os._exit(0)  # the event thread is blocked in a long poll; do not outlive the session
