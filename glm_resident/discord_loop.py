"""Discord event loop — the second room Kestrel lives in.

The sibling of `zulip_loop.run`, and deliberately not a generalization of
it: see backend.py on why the event models are not worth unifying. What the
two loops share is everything after the message dict — the judge, the
prompt, the engine, the tools, the state dir, the handled ledger, the
distiller, the session reaper. This module's whole job is to turn gateway
callbacks into that dict and feed them in at the same door.

## Three threads, because a turn takes longer than a heartbeat

    gateway thread   discord.py's event loop. Must never block: a stalled
                     heartbeat is a dropped connection. `on_message` does
                     nothing but normalize and append.
    worker thread    pops the queue and runs `resident.handle_message` —
                     20-150s of blocking work per turn.
    background       the inherited sidecars (reaper, distiller, solitude),
                     unchanged and platform-blind.

The queue between gateway and worker is what makes the resident able to
think for two minutes in a room that keeps talking. It also gives the
burst-batching the harness already does for free: everything that piles up
in one channel while he is mid-turn is handed to the NEXT turn as
followups, so he answers a conversation rather than replaying it line by
line.
"""

import sys
import json
import time
import logging
import threading
from collections import deque

from glm_resident.config import METACOG_ENABLED, POSTMARK_ENABLED
from glm_resident.metacog import MetacogRunner
from glm_resident.sandbox import ensure_bg_container, stop_bg_container
from glm_resident.session_summary import start_session_reaper
from glm_resident.distill import start_distiller
from glm_resident.solitude import start_solitude
from glm_resident.tools.postmark_tools import start_postmark_refresher
from glm_resident.util import safe_filename
from glm_resident.zulip_loop import _msg_key

logger = logging.getLogger("discord_loop")

# How many messages of each channel to pull on boot, mirroring
# BACKFILL_PER_STANDING_STREAM. Discord's history API is per-channel and
# rate-limited, so this stays modest.
BACKFILL_PER_CHANNEL = 100


def backfill_history(resident, channels: list[str] | None = None):
    """Seed the channel logs so a cold start has a room to read.

    Same purpose as the Zulip backfill: the distiller and every cold-start
    prompt read `channels/{stream}/{topic}.jsonl`, and on a first boot
    those are empty. Without this he arrives in a room with no past.
    """
    result = resident.chat.get_streams()
    if result.get("result") != "success":
        logger.warning(f"Could not list Discord channels: {result}")
        return
    names = [s["name"] for s in result.get("streams", [])]
    if channels:
        names = [n for n in names if n in channels]
    total = 0
    for name in names:
        res = resident.chat.get_messages({
            "anchor": "newest", "num_before": BACKFILL_PER_CHANNEL,
            "num_after": 0,
            "narrow": [{"operator": "channel", "operand": name}],
        })
        if res.get("result") != "success":
            logger.warning(f"Could not backfill #{name}: {res.get('msg')}")
            continue
        msgs = res.get("messages", [])
        if not msgs:
            continue
        # The room as HIS memory names it (see config.DISCORD_STREAM_PREFIX).
        stream = resident.chat.stream_name(name)
        # Dedup against what is ALREADY on disk, the same way the Zulip
        # backfill does. `resident._msg_index` is not the guard to use: it
        # is an in-memory dict capped at a few thousand entries and empty
        # on every boot, so leaning on it would re-append the whole tail of
        # every channel to the logs at each restart.
        existing = _logged_ids(resident, stream)
        new_count = 0
        for m in msgs:
            mid = m.get("id")
            topic = m.get("subject", name)
            if mid and mid in existing.get(safe_filename(topic), set()):
                continue
            resident.state.log_message(
                stream=m.get("display_recipient", stream),
                topic=topic,
                sender=m.get("sender_full_name", "unknown"),
                content=m.get("content", ""),
                timestamp=_iso(m.get("timestamp", 0)),
                msg_id=mid,
            )
            new_count += 1
            if mid:
                resident._msg_index[mid] = {
                    "stream": m.get("display_recipient", stream),
                    "topic": topic,
                    "sender": m.get("sender_full_name", "unknown"),
                    "preview": m.get("content", "")[:80].replace("\n", " "),
                }
        total += new_count
        logger.info(f"Backfilled #{name}: {new_count} new of {len(msgs)} "
                    f"fetched")
    logger.info(f"Discord backfill: {total} new message(s) logged")


def _logged_ids(resident, stream: str) -> dict:
    """{topic_filename: {msg_id, ...}} already in this stream's logs."""
    out: dict = {}
    stream_dir = resident.state.root / f"channels/{safe_filename(stream)}"
    if not stream_dir.exists():
        return out
    for log_file in stream_dir.glob("*.jsonl"):
        ids = set()
        for line in log_file.read_text().strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("msg_id"):
                ids.add(entry["msg_id"])
        out[log_file.stem] = ids
    return out


def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts or 0, timezone.utc).isoformat()


def mark_history_handled(resident) -> int:
    """First boot on Discord: read the past, do not answer it.

    The same rule `zulip_loop.replay_pending` applies via
    HandledSet.pristine — replay bridges downtime, and a resident who has
    never been in this server has none. Applied per-platform because the
    ledger is shared: Kestrel may be long-established on Zulip (ledger not
    pristine) and brand new to Discord on the same boot, so Discord's
    history has to be marked on its own terms.
    """
    result = resident.chat.get_streams()
    if result.get("result") != "success":
        return 0
    marked = 0
    for s in result.get("streams", []):
        res = resident.chat.get_messages({
            "anchor": "newest", "num_before": 50, "num_after": 0,
            "narrow": [{"operator": "channel", "operand": s["name"]}],
        })
        for m in res.get("messages", []):
            if m.get("id") not in resident.handled:
                resident.handled.mark(m["id"])
                marked += 1
    return marked


def run(resident, channels: list[str] | None = None,
        mark_first_boot: bool = True):
    """Main Discord loop. `resident.chat` must be a started DiscordBackend."""
    backend = resident.chat
    profile = backend.get_profile()
    logger.info(f"Kestrel on Discord as {profile.get('full_name')}")
    logger.info(f"State directory: {resident.state.root}")
    logger.info(f"Model: {resident.model}")

    backfill_history(resident, channels)

    try:
        ensure_bg_container(resident.state.root)
    except Exception as e:
        logger.warning(f"Background container setup failed: {e}")
    try:
        resident.state._copy_harness_source()
    except Exception:
        pass

    if mark_first_boot:
        n = mark_history_handled(resident)
        logger.info(f"Discord first boot — marked {n} message(s) handled "
                    f"instead of replying. He starts listening from now.")

    if METACOG_ENABLED:
        runner = MetacogRunner(resident)
        resident._metacog_runner = runner
        runner.start()
    start_session_reaper(resident)
    if POSTMARK_ENABLED:
        start_postmark_refresher(resident.state)
    start_distiller(resident)
    start_solitude(resident)

    pending: deque = deque()
    # Channels are named by the user the way Discord names them
    # ("general"); normalized messages carry the prefixed stream name
    # ("dc-general"). Compare in one namespace or the filter silently
    # drops everything.
    allowed = ({backend.stream_name(backend.channel_name(c))
                for c in channels} if channels else None)

    # --- gateway side: normalize and hand off, never block -------------
    @backend.client.event
    async def on_message(message):
        try:
            if message.author.id == backend.client.user.id:
                return                      # his own posts
            norm = backend.normalize(message)
            if allowed is not None and norm["type"] == "stream" \
                    and norm["display_recipient"] not in allowed:
                return
            pending.append(norm)
        except Exception as e:
            logger.error(f"on_message normalize failed: {e}", exc_info=True)

    @backend.client.event
    async def on_raw_reaction_add(payload):
        try:
            if payload.user_id == backend.client.user.id:
                return
            emoji = getattr(payload.emoji, "name", None) or str(payload.emoji)
            resident._handle_reaction_event({
                "type": "reaction", "op": "add",
                "message_id": payload.message_id,
                "emoji_name": emoji,
                "user_id": payload.user_id,
            })
        except Exception as e:
            logger.debug(f"reaction event ignored: {e}")

    logger.info("Discord loop listening"
                + (f" on {sorted(allowed)}" if allowed else " on all channels"))

    # --- worker side: the slow part ------------------------------------
    def _take_matching(key) -> list[dict]:
        matches = [m for m in pending if _msg_key(m) == key]
        if matches:
            rest = [m for m in pending if _msg_key(m) != key]
            pending.clear()
            pending.extend(rest)
        return matches

    def _sweep(key) -> list[dict]:
        # No fetch needed, unlike Zulip: the gateway has already pushed
        # anything that arrived while he was thinking.
        return _take_matching(key)

    last_error = ["", 0]
    try:
        while True:
            try:
                if not pending:
                    time.sleep(0.25)
                    if resident._restart_requested:
                        logger.info("Self-edit detected — exiting for restart")
                        stop_bg_container()
                        sys.exit(42)
                    continue
                msg = pending.popleft()
                key = _msg_key(msg)
                followups = _take_matching(key)
                try:
                    resident.handle_message(msg, followups=followups,
                                            sweep=lambda k=key: _sweep(k))
                except Exception as e:
                    logger.error(f"Error handling batch in "
                                 f"#{key[0]}>{key[1]}: {e}", exc_info=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                sig = str(e)[:120]
                if sig == last_error[0]:
                    last_error[1] += 1
                    if last_error[1] % 40 == 0:
                        logger.error(f"Discord loop error x{last_error[1]}: {sig}")
                else:
                    last_error = [sig, 1]
                    logger.error(f"Discord loop error: {e}", exc_info=True)
                time.sleep(2)
    except KeyboardInterrupt:
        logger.info("Shutting down Discord loop")
    finally:
        backend.stop()
        stop_bg_container()


def run_threaded(resident, channels: list[str] | None = None,
                 mark_first_boot: bool = True) -> threading.Thread:
    """Run the Discord loop on a thread, for a resident who also runs a
    Zulip loop in the foreground. This is the dual-presence entry point:
    ONE resident object, one state dir, two loops.

    The sidecars (reaper, distiller, solitude) are started by BOTH loops'
    startup paths, and starting them twice would double every background
    sweep — two distillers paying twice for the same era volumes and racing
    on the same hits files, two solitude threads taking two uninvited
    turns. They are therefore routed through util.start_daemon_once, which
    starts one named thread per process and logs the second attempt. So
    whichever loop comes up first wins, and the other's call is a no-op.
    """
    t = threading.Thread(
        target=run, args=(resident, channels, mark_first_boot),
        name="discord-loop", daemon=True)
    t.start()
    return t
