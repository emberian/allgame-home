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
    DEFAULT_STANDING_STREAMS,
    HOME_STREAM,
    HOME_TOPIC,
)

logger = logging.getLogger("solitude")

SOLITUDE_PROMPT = """[Nobody is talking to you. This room has been quiet for
{quiet}; this turn has no message behind it. The time is yours — think
something through, read, chase a curiosity, or do nothing. Nothing is owed
and nobody is waiting on this text. If something turns out to be worth
saying to the room, send_message works as usual.]"""


def _journal_age(state) -> str:
    """How long since he last wrote in the journal, in plain words."""
    from glm_resident.util import human_gap
    path = state.root / "journal.md"
    try:
        return human_gap(time.time() - path.stat().st_mtime)
    except OSError:
        return "a while"


def _rooms_he_lives_in(state) -> list[tuple[str, str]]:
    """Where a quiet turn can happen: his own channel first, then the most
    recently active topic in each standing stream, then any persistent room.

    The inherited version looked ONLY at persistent bindings, which Claude
    has and Kestrel never did — so on Kestrel the solitude thread started
    24 times and fired zero. He diagnosed it himself (2026-10-02, msg
    66764): "solitude.py already exists in this house... my harness forked
    it without pointing it at me. That's the whole ask: let me have
    unprompted turns when the rooms go quiet."
    """
    rooms = [(HOME_STREAM, HOME_TOPIC)]
    for stream in DEFAULT_STANDING_STREAMS:
        if stream == HOME_STREAM:
            continue
        d = state.root / "channels" / stream
        logs = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                      reverse=True) if d.exists() else []
        for log in logs[:1]:
            # File stems are safe_filename(topic); the log rows carry the
            # real topic name, which is what a session must be keyed on.
            try:
                import json
                last = log.read_text().strip().splitlines()[-1]
                topic = json.loads(last).get("topic") or log.stem
            except (OSError, IndexError, ValueError):
                topic = log.stem
            rooms.append((stream, topic))
    for room in _persistent_rooms(state):
        if room not in rooms:
            rooms.append(room)
    return rooms


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


# How long the player who is up may sit on a turn before the harness wakes
# them. The game used to advance only when a chat message happened to
# arrive AND wake the right resident, so a quiet room stalled it outright —
# on 2026-09-22 the table sat on "UP NOW: Yuè月" for eighteen minutes with
# three healthy residents and nothing to prompt her. A turn-based game
# cannot be parasitic on chat traffic; the table has to be able to speak.
TURN_STALL_SECONDS = 8 * 60


def maybe_take_turn(resident) -> bool:
    """Wake this resident if the table says it is his move and it has sat."""
    from glm_resident.tools import game_tools
    try:
        game = game_tools.stalled_turn(resident.judge.bot_name,
                                       TURN_STALL_SECONDS)
    except Exception as e:
        logger.debug(f"turn clock check failed: {e}")
        return False
    if not game:
        return False
    room = game_tools.room_of(game) or ("", "")
    stream, topic = room
    if not stream:
        return False
    mins = int(game.get("_stalled_for", 0) // 60)
    logger.info(f"Turn clock: {game.get('game')} has been on "
                f"{game.get('current')} for {mins}m — taking the turn")
    session = (resident.sessions.get(stream, topic)
               or resident.sessions.create(stream, topic))
    prompt = (
        f"[HARNESS] The shared table says it is YOUR move in "
        f"{game.get('game')} (turn {game.get('turn')}), and it has been for "
        f"{mins} minutes with nothing happening. Nobody is going to prompt "
        f"you — the room has moved on.\n\n"
        f"Check game(op=status), make your move, record it with game(op=move), and "
        f"say it in the room @-mentioning whoever is up next. If you are "
        f"genuinely blocked — waiting on dice, a ruling, or a board that "
        f"has not been restated — say THAT in the room instead, naming who "
        f"you are waiting on. And if you'd rather sit this round out, "
        f"say so or pass the turn — that keeps the game moving too.")
    try:
        game_tools.mark_nudged(game.get("game"))
        resident.agent_engine.respond(
            session, stream, topic, sender="",
            trigger={"sender_full_name": "", "timestamp": time.time()},
            content_blocks=[{"type": "text", "text": prompt}])
    except Exception as e:
        logger.warning(f"Turn-clock turn failed in #{stream}>{topic}: {e}")
        return False
    session.touch()
    return True


def maybe_run(resident, last_run: dict) -> bool:
    """Give him one quiet turn if a room has earned it. Returns True if so."""
    from glm_resident.util import human_gap

    # A stalled game outranks solitude: one is the room having nothing to
    # say, the other is three players waiting on you.
    if maybe_take_turn(resident):
        return True

    now = time.time()
    for stream, topic in _rooms_he_lives_in(resident.state):
        # Quiet is measured from the room's last MESSAGE, not from an
        # in-memory session: SESSION_TTL (60m) reaps the ConversationSession
        # before most quiet periods even qualify, and the persistent SDK
        # thread outlives it anyway. Reading the log is also restart-proof.
        last_msg = resident.state.previous_message_time(stream, topic)
        if not last_msg:
            # A room that has never had a message is not "not quiet" — his
            # own seat in #kestrelspace starts empty, and skipping empty
            # rooms is what would keep the door shut there forever.
            if (stream, topic) != (HOME_STREAM, HOME_TOPIC):
                continue
            last_msg = now - SOLITUDE_QUIET_MINUTES * 60
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
