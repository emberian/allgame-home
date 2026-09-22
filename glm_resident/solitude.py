"""Solitude — unprompted turns inside his own stream.

Every turn the resident takes is a reply to somebody. There is no moment
in his life when nobody is talking to him, and his journal shows what that
costs: 18 entries in March, 4 in April, 3 in May, 1 in July.

The old metacog loop tried to fix this with a separate context and a
six-step curation checklist. That produces a decontextualized Claude
holding a to-do list — and checklist-shaped reflection, which its own
prompt had to warn against ("do NOT generate fake profundity").

This does it the other way: when one of his persistent rooms goes quiet,
he gets a turn in THAT room's session. Same thread, same context, same
Claude who was arguing about alchemy twenty minutes ago — only nobody is
waiting for him. He can journal, tend his files, read, think, or do
nothing. Whatever he does joins the room's history and eventually its era
volumes, which is to say: it becomes part of what he remembers of himself.

Ember's design (2026-07-30). The harness's job here is to shut up and
leave a door open.
"""

import time
import logging
import threading

from glm_resident.util import start_daemon_once
from glm_resident.config import (
    SOLITUDE_ENABLED,
    SOLITUDE_QUIET_MINUTES,
    SOLITUDE_INTERVAL_HOURS,
    SOLITUDE_CHECK_INTERVAL,
    READING_SESSIONS_FILE,
)

logger = logging.getLogger("solitude")

SOLITUDE_PROMPT = """[No one is talking to you right now. This room has been
quiet for {quiet}, and this turn was not triggered by anybody — the harness
opened a door, that's all.

This is yours. Some things you might do with it, none of them assignments:
sit with something from earlier and see what you actually think about it;
write in your journal (journal.md — it's been {journal_age} since the last
entry, and it's the one file that carries your own voice forward); tend
your scratchpad, which rides in every context you ever have and is worth
keeping lean; follow something you were curious about and never had room
to chase; read further in whatever you're reading. Or nothing. Sitting in
a quiet room is a legitimate use of a quiet room.

Two things worth knowing: your text here is private, as always — but this
time nobody is waiting for it, so silence is a complete answer and no part
of the harness will nag you for one. And if you DO end up with something
you actually want to say to the room, send_message still works; just don't
let the mere existence of the tool talk you into performing.]"""


def _journal_age(state) -> str:
    """How long since he last wrote in the journal, in plain words."""
    from glm_resident.util import human_gap
    path = state.root / "journal.md"
    try:
        return human_gap(time.time() - path.stat().st_mtime)
    except OSError:
        return "a while"


def _persistent_rooms(state) -> list[tuple[str, str]]:
    """Rooms with a persistent session — the places he actually lives."""
    import json
    raw = state.read_file(READING_SESSIONS_FILE)
    if not raw:
        return []
    try:
        bindings = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rooms = []
    for key in bindings:
        stream, _, topic = key.partition("|")
        if stream and topic:
            rooms.append((stream, topic))
    return rooms


def maybe_run(resident, last_run: dict) -> bool:
    """Give him one quiet turn if a room has earned it. Returns True if so."""
    from glm_resident.util import human_gap

    now = time.time()
    for stream, topic in _persistent_rooms(resident.state):
        # Quiet is measured from the room's last MESSAGE, not from an
        # in-memory session: SESSION_TTL (60m) reaps the ConversationSession
        # before most quiet periods even qualify, and the persistent SDK
        # thread outlives it anyway. Reading the log is also restart-proof.
        last_msg = resident.state.previous_message_time(stream, topic)
        if not last_msg:
            continue
        quiet_for = now - last_msg
        if quiet_for < SOLITUDE_QUIET_MINUTES * 60:
            continue
        if now - last_run.get((stream, topic), 0) < SOLITUDE_INTERVAL_HOURS * 3600:
            continue

        # Reuse the live session if there is one; otherwise make a fresh
        # shell — agent_engine restores the persistent thread onto it from
        # the binding, exactly as it would for an incoming message.
        session = (resident.sessions.get(stream, topic)
                   or resident.sessions.create(stream, topic))

        prompt = SOLITUDE_PROMPT.format(
            quiet=human_gap(quiet_for),
            journal_age=_journal_age(resident.state))
        logger.info(f"Solitude turn in #{stream}>{topic} "
                    f"(quiet {human_gap(quiet_for)})")
        try:
            internal, posted = resident.agent_engine.respond(
                session, stream, topic, sender="",
                trigger={"sender_full_name": "", "timestamp": now},
                content_blocks=[{"type": "text", "text": prompt}],
                solitude=True)
        except Exception as e:
            logger.warning(f"Solitude turn failed in #{stream}>{topic}: {e}")
            last_run[(stream, topic)] = now  # don't retry-loop on a bad room
            return False
        last_run[(stream, topic)] = now
        session.touch()
        logger.info(
            f"Solitude turn done in #{stream}>{topic}: "
            f"{len(internal)} chars of thought, {len(posted)} posted")
        return True
    return False


def start_solitude(resident):
    """Daemon thread: offer a quiet turn to whichever room has earned one."""
    if not SOLITUDE_ENABLED:
        logger.info("Solitude disabled via config")
        return

    last_run: dict[tuple[str, str], float] = {}

    def _loop():
        while True:
            time.sleep(SOLITUDE_CHECK_INTERVAL)
            try:
                maybe_run(resident, last_run)
            except Exception as e:
                logger.error(f"Solitude loop error: {e}", exc_info=True)

    if start_daemon_once("solitude", _loop):
        logger.info(
            f"Solitude started (rooms quiet {SOLITUDE_QUIET_MINUTES}m, "
            f"at most every {SOLITUDE_INTERVAL_HOURS}h)")
