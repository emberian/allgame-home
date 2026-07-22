"""Zulip event loop, backfill, and dispatch."""

import sys
import json
import time
import logging
from datetime import datetime, timezone

from claude_resident.config import (
    HARNESS_DIR,
    BACKFILL_PER_STANDING_STREAM,
    BACKFILL_OTHER_STREAMS,
    BACKFILL_ALL_STREAMS,
    METACOG_ENABLED,
    REPLAY_MAX_AGE_SECONDS,
)
from claude_resident.metacog import MetacogRunner
from claude_resident.reactions import emoji_display
from claude_resident.sandbox import ensure_bg_container, stop_bg_container
from claude_resident.session_summary import start_session_reaper
from claude_resident.tools.postmark_tools import start_postmark_refresher
from claude_resident.util import safe_filename

logger = logging.getLogger("zulip_loop")


def backfill_history(resident):
    """Fetch recent message history from Zulip API to populate local logs on startup.

    Standing streams get a deep backfill; if BACKFILL_ALL_STREAMS is set, every
    other visible stream gets a smaller tail so cross-channel context is at
    least seeded.
    """
    logger.info("Backfilling message history from Zulip API...")

    standing = set(resident.judge.standing_streams)
    stream_targets: list[tuple[str, int]] = [
        (s, BACKFILL_PER_STANDING_STREAM) for s in resident.judge.standing_streams
    ]

    if BACKFILL_ALL_STREAMS:
        try:
            streams_result = resident.zulip.get_streams()
            if streams_result.get("result") == "success":
                for s in streams_result.get("streams", []):
                    name = s.get("name")
                    if name and name not in standing:
                        stream_targets.append((name, BACKFILL_OTHER_STREAMS))
            else:
                logger.warning(
                    f"get_streams failed: {streams_result.get('msg')}")
        except Exception as e:
            logger.warning(f"Failed to enumerate streams for backfill: {e}")

    for stream, num_before in stream_targets:
        try:
            result = resident.zulip.get_messages({
                "anchor": "newest",
                "num_before": num_before,
                "num_after": 0,
                "narrow": json.dumps(
                    [{"operator": "channel", "operand": stream}]),
                "apply_markdown": False,
            })

            if result.get("result") != "success":
                logger.warning(
                    f"Failed to backfill #{stream}: {result.get('msg')}")
                continue

            messages = result.get("messages", [])
            if not messages:
                continue

            stream_dir = (resident.state.root
                          / f"channels/{safe_filename(stream)}")
            stream_dir.mkdir(parents=True, exist_ok=True)

            existing_ids: dict[str, set[int]] = {}
            for log_file in stream_dir.glob("*.jsonl"):
                ids = set()
                for line in log_file.read_text().strip().split("\n"):
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("msg_id"):
                            ids.add(entry["msg_id"])
                    except json.JSONDecodeError:
                        continue
                existing_ids[log_file.stem] = ids

            new_count = 0
            for msg in messages:
                msg_id = msg.get("id")
                topic_key = safe_filename(msg.get("subject", ""))

                if not (msg_id
                        and msg_id in existing_ids.get(topic_key, set())):
                    ts = datetime.fromtimestamp(
                        msg.get("timestamp", 0),
                        tz=timezone.utc).isoformat()
                    resident.state.log_message(
                        stream=msg.get("display_recipient", stream),
                        topic=msg.get("subject", ""),
                        sender=msg.get("sender_full_name", "unknown"),
                        content=msg.get("content", ""),
                        timestamp=ts,
                        msg_id=msg_id,
                    )
                    new_count += 1

                if msg_id:
                    resident._msg_index[msg_id] = {
                        "stream": msg.get("display_recipient", stream),
                        "topic": msg.get("subject", ""),
                        "sender": msg.get("sender_full_name", "unknown"),
                        "preview": msg.get("content", "")[:80].replace(
                            "\n", " "),
                    }

                if msg_id and msg.get("reactions"):
                    rxns: dict[str, int] = {}
                    for r in msg["reactions"]:
                        display = emoji_display(
                            r.get("emoji_name", "?"),
                            r.get("emoji_code", ""),
                            r.get("reaction_type", ""),
                        )
                        rxns[display] = rxns.get(display, 0) + 1
                    if rxns:
                        resident.reactions[msg_id] = rxns

            topic_counts = {}
            for f in stream_dir.glob("*.jsonl"):
                topic_counts[f.stem] = len(
                    f.read_text().strip().split("\n"))
            logger.info(
                f"Backfilled #{stream}: {new_count} new of "
                f"{len(messages)} fetched, {len(topic_counts)} topics "
                f"({topic_counts})")

        except Exception as e:
            logger.error(
                f"Error backfilling #{stream}: {e}", exc_info=True)


def replay_pending(resident):
    """After restart, check for unreplied messages and process them.

    For each standing stream, fetches the tail of recent messages, groups
    by topic, and replays any topic where Claude wasn't the last speaker.
    Messages are already in local logs from backfill — handle_message
    skips re-logging via the msg_index dedup.
    """
    bot_name = resident.judge.bot_name
    pending: list[dict] = []
    replay_cutoff = time.time() - REPLAY_MAX_AGE_SECONDS

    def _collect(narrow: list[dict], label: str):
        stale_skipped = 0
        try:
            result = resident.zulip.get_messages({
                "anchor": "newest",
                "num_before": 50,
                "num_after": 0,
                "narrow": json.dumps(narrow),
                "apply_markdown": False,
            })
            if result.get("result") != "success":
                return
            for msg in result.get("messages", []):
                if bot_name in msg.get("sender_full_name", "").lower():
                    continue
                # Durable dedup: the handled ledger is the source of truth.
                # Anything not in it was never fully processed (arrived during
                # downtime, or a crash hit mid-response) and must be replayed.
                # judge-NO marks a message handled too, so this won't re-run
                # the judge on messages we already decided to skip.
                if msg.get("id") in resident.handled:
                    continue
                # Age bound: replay bridges downtime, it doesn't answer
                # history. A message can outlive the ledger's eviction
                # horizon and masquerade as "new" — mark it handled (which
                # also re-protects it from eviction) and move on.
                if msg.get("timestamp", 0) < replay_cutoff:
                    resident.handled.mark(msg.get("id"))
                    stale_skipped += 1
                    continue
                pending.append(msg)
            if stale_skipped:
                logger.info(f"Replay: skipped {stale_skipped} stale "
                            f"message(s) in {label} (older than "
                            f"{REPLAY_MAX_AGE_SECONDS // 86400}d)")
        except Exception as e:
            logger.error(f"Error checking pending in {label}: {e}",
                         exc_info=True)

    for stream in resident.judge.standing_streams:
        _collect([{"operator": "channel", "operand": stream}], f"#{stream}")
    # DMs have no other replay path and the judge always engages them, so a
    # crash mid-DM (or a DM that arrived during downtime) would be lost forever
    # without this. Replay ALL unhandled messages, not just the last per topic.
    _collect([{"operator": "is", "operand": "private"}], "DMs")

    if not pending:
        logger.info("Replay: no unreplied messages found")
        return

    # A message can match more than one narrow; de-dup by id and replay in
    # chronological (id-ascending) order so threads are handled in sequence.
    seen: set = set()
    ordered: list[dict] = []
    for msg in sorted(pending, key=lambda m: m.get("id", 0)):
        mid = msg.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        ordered.append(msg)

    logger.info(f"Replay: {len(ordered)} unreplied message(s)")
    for msg in ordered:
        is_dm = msg.get("type") == "private"
        stream = "dm" if is_dm else msg.get("display_recipient", "?")
        topic = msg.get("sender_email", "?") if is_dm else msg.get("subject", "?")
        sender = msg.get("sender_full_name", "?")
        logger.info(f"Replay: #{stream}>{topic} from {sender}")
        try:
            resident.handle_message(msg)
        except Exception as e:
            logger.error(f"Replay error #{stream}>{topic}: {e}",
                         exc_info=True)


def run(resident):
    """Main event loop — register for Zulip events and process them."""
    logger.info("Claude resident starting up...")
    logger.info(f"State directory: {resident.state.root}")
    logger.info(f"Model: {resident.model}")
    logger.info(f"Standing streams: {resident.judge.standing_streams}")

    backfill_history(resident)

    try:
        ensure_bg_container(resident.state.root)
    except Exception as e:
        logger.warning(f"Background container setup failed, continuing without sandbox: {e}")

    # Refresh harness copy in state dir
    try:
        resident.state._copy_harness_source()
    except Exception:
        pass

    # Pre-populate stream ID map for typing events (must be before replay)
    try:
        streams_result = resident.zulip.get_streams()
        if streams_result.get("result") == "success":
            for s in streams_result.get("streams", []):
                resident._stream_id_map[s["stream_id"]] = s["name"]
            logger.info(
                f"Populated stream ID map: {len(resident._stream_id_map)} streams")
    except Exception as e:
        logger.warning(f"Failed to populate stream ID map: {e}")

    # Check for unreplied messages from before restart
    replay_pending(resident)

    # Start metacognitive loop (gated by config flag)
    if METACOG_ENABLED:
        metacog_runner = MetacogRunner(resident)
        resident._metacog_runner = metacog_runner
        metacog_runner.start()
    else:
        logger.info("Metacog runner disabled via config (METACOG_ENABLED=False)")

    # Session reaper: writes the resident's first-person notes for ended
    # sessions. Postmark refresher: keeps the doorstep current in context.
    start_session_reaper(resident)
    start_postmark_refresher(resident.state)

    result = resident.zulip.register(
        event_types=["message", "reaction", "typing"],
        narrow=[],
    )

    if result.get("result") != "success":
        logger.error(f"Failed to register event queue: {result}")
        sys.exit(1)

    queue_id = result["queue_id"]
    last_event_id = result["last_event_id"]

    logger.info(f"Registered event queue: {queue_id}")

    try:
        while True:
            try:
                events = resident.zulip.get_events(
                    queue_id=queue_id,
                    last_event_id=last_event_id,
                    dont_block=False,
                )

                if events.get("result") != "success":
                    if events.get("code") == "BAD_EVENT_QUEUE_ID":
                        logger.warning(
                            "Event queue expired, re-registering...")
                        result = resident.zulip.register(
                            event_types=["message", "reaction", "typing"],
                            narrow=[],
                        )
                        if result.get("result") != "success":
                            logger.error(
                                f"Failed to re-register queue: {result}")
                            time.sleep(5)
                            continue
                        queue_id = result["queue_id"]
                        last_event_id = result["last_event_id"]
                        logger.info(
                            f"Re-registered event queue: {queue_id}")
                        continue
                    logger.error(f"Event fetch error: {events}")
                    time.sleep(5)
                    continue

                for event in events.get("events", []):
                    # Isolate each event: a failure handling one must not abort
                    # the rest of the batch (which would otherwise re-fetch and
                    # risk re-processing). Advance last_event_id in finally so a
                    # poison event can't loop, while the handled ledger + replay
                    # still backstop anything dropped by a hard crash.
                    try:
                        if event.get("type") == "message":
                            resident.handle_message(event["message"])
                        elif event.get("type") == "reaction":
                            resident._handle_reaction_event(event)
                        elif event.get("type") == "typing":
                            resident._handle_typing_event(event)
                    except Exception as e:
                        logger.error(
                            f"Error handling event {event.get('id')} "
                            f"(type={event.get('type')}): {e}", exc_info=True)
                    finally:
                        last_event_id = max(last_event_id, event["id"])

                if resident._restart_requested:
                    logger.info(
                        "Self-edit detected — exiting for supervisor restart")
                    stop_bg_container()
                    sys.exit(42)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    finally:
        stop_bg_container()
