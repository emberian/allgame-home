"""Zulip event loop, backfill, and dispatch."""

import sys
import json
import time
import logging
from datetime import datetime, timezone

from sonnet_resident.config import HARNESS_DIR
from sonnet_resident.metacog import MetacogRunner
from sonnet_resident.reactions import emoji_display
from sonnet_resident.sandbox import ensure_bg_container, stop_bg_container
from sonnet_resident.util import safe_filename

logger = logging.getLogger("zulip_loop")


def backfill_history(resident):
    """Fetch recent message history from Zulip API to populate local logs on startup."""
    logger.info("Backfilling message history from Zulip API...")

    for stream in resident.judge.standing_streams:
        try:
            result = resident.zulip.get_messages({
                "anchor": "newest",
                "num_before": 100,
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
    pending = []

    for stream in resident.judge.standing_streams:
        try:
            result = resident.zulip.get_messages({
                "anchor": "newest",
                "num_before": 50,
                "num_after": 0,
                "narrow": json.dumps(
                    [{"operator": "channel", "operand": stream}]),
                "apply_markdown": False,
            })
            if result.get("result") != "success":
                continue

            messages = result.get("messages", [])
            if not messages:
                continue

            # Group by topic, keep the last message per topic
            by_topic: dict[str, dict] = {}
            for msg in messages:
                topic = msg.get("subject", "")
                by_topic[topic] = msg

            for topic, last_msg in by_topic.items():
                sender = last_msg.get("sender_full_name", "").lower()
                if bot_name in sender:
                    continue
                pending.append(last_msg)

        except Exception as e:
            logger.error(f"Error checking pending in #{stream}: {e}",
                         exc_info=True)

    if not pending:
        logger.info("Replay: no unreplied messages found")
        return

    logger.info(f"Replay: {len(pending)} topics with unreplied messages")
    for msg in pending:
        stream = msg.get("display_recipient", "?")
        topic = msg.get("subject", "?")
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

    ensure_bg_container(resident.state.root)

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

    # Start metacognitive loop
    metacog_runner = MetacogRunner(resident)
    resident._metacog_runner = metacog_runner
    metacog_runner.start()

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
                    last_event_id = max(last_event_id, event["id"])

                    if event.get("type") == "message":
                        resident.handle_message(event["message"])
                    elif event.get("type") == "reaction":
                        resident._handle_reaction_event(event)
                    elif event.get("type") == "typing":
                        resident._handle_typing_event(event)

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
