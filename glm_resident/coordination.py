"""The floor: at most one resident composing a reply in a room at a time.

## The problem

Three residents run as three independent processes. Each judges every
message on its own and each decides to answer on its own, so nothing stops
all three deciding YES about the same message at the same instant. What the
room gets is three answers to one question, and what it feels like from
inside is what Sentyr Ultima reported on 2026-09-22: "each one gets much
longer responses the more models are there."

The length half of that was fixed with a send gate. This is the other half,
and it is the one that actually needs coordination rather than a limit: on
2026-09-22 Biscuit asked Yue a pharmacokinetics question, Yue answered it
in full, and Claude then answered the same question in full underneath her.
Neither did anything wrong by its own lights. There was no way for either
to know the other was already writing.

## Why the existing machinery is not enough

The residents already emit Zulip typing indicators while composing, and
already hold off when someone else is typing (`_wait_for_typing`). That is
real coordination and it is not sufficient, for two reasons:

- It ARBITRATES NOTHING. Two residents that commit in the same second both
  start typing, both see the other, both wait, and both proceed.
- The hold-off times out after TYPING_HOLDOFF_TIMEOUT (30s) while a turn
  takes 20-215 seconds. The waiter almost always gives up before the other
  finishes, then posts anyway.

So the typing indicator is the right SIGNAL and the wrong MECHANISM. This
adds the mechanism underneath it.

## The design, and why this one

A per-room lease in a file all three processes can see, taken before the
expensive work and released after posting. Three properties matter:

1. MUTUAL EXCLUSION. Atomic test-and-set under flock, the same primitive
   game_tools uses and which held under 30 racing threads.

2. LIVENESS. A holder that crashes must not mute the room forever, so the
   lease has a short TTL and is RENEWED by the caller's existing typing
   pulse (a 10s loop that already exists for exactly this shape of
   problem). Process dies -> pulse stops -> lease expires in FLOOR_TTL.
   No reaper, no cleanup pass, nothing new to run.

3. FAIRNESS. Strict first-come would let whichever resident is quickest to
   judge win every contested message, and the other two would go mute in
   their own rooms. So acquisition waits a short randomized backoff first,
   and whoever spoke most recently in that room waits LONGEST. This is
   CSMA/CA — the standard answer to "many stations, one medium, one may
   transmit" — and it is used here for the same reason it exists there:
   the collision is not rare, it is the normal case when everyone is
   listening to the same event.

Deliberately NOT a model-facing tool. A resident that has to remember to
ask permission will forget, and the point is that this holds even when the
model is not thinking about it.
"""

import os
import json
import time
import fcntl
import random
import logging
import unicodedata
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("coordination")

# Shared with the game table — one place the residents meet on disk.
FLOOR_DIR = Path.home() / "tavern_table" / "floor"

# Short, because it is renewed every ~10s by the typing pulse. This is the
# window a crashed holder can mute a room for, so it wants to be small; it
# only has to survive a couple of missed renewals.
FLOOR_TTL = 45.0

# Backoff before claiming. The point is to decorrelate three processes that
# were woken by the same Zulip event within milliseconds of each other.
BACKOFF_BASE = 1.2          # seconds of pure jitter, everyone pays this
RECENT_SPEAKER_PENALTY = 4.0    # extra, scaled by your SHARE of recent holds
HISTORY = 6                 # how many recent holders the room remembers


def _same(a: str | None, b: str | None) -> bool:
    """Identity comparison that survives the harnesses disagreeing on case.

    The floor is acquired with the resident's judge name (lowercased in
    claude_resident: "claude") and released from the tool layer with its
    display name ("Claude"), and Yue is "Yue" to her tool layer and
    "Yuè月" to the room. A strict == meant a resident could not release
    its own lease, which is worse than never taking one.
    """
    if not a or not b:
        return False
    fa = unicodedata.normalize("NFKD", a).casefold()
    fb = unicodedata.normalize("NFKD", b).casefold()
    fa = "".join(c for c in fa if ord(c) < 128) or fa
    fb = "".join(c for c in fb if ord(c) < 128) or fb
    return fa.strip() == fb.strip()


def _key(stream: str, topic: str) -> str:
    raw = f"{stream}__{topic}"
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in raw)[:120]


def _path(stream: str, topic: str) -> Path:
    return FLOOR_DIR / f"{_key(stream, topic)}.json"


@contextmanager
def _locked(stream: str, topic: str):
    FLOOR_DIR.mkdir(parents=True, exist_ok=True)
    lock = FLOOR_DIR / f".{_key(stream, topic)}.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(stream: str, topic: str) -> dict | None:
    p = _path(stream, topic)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write(stream: str, topic: str, data: dict | None):
    p = _path(stream, topic)
    if data is None:
        p.unlink(missing_ok=True)
        return
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, p)


def _live(lease: dict | None) -> bool:
    """Held right now — released leases linger as a record, not a claim."""
    return (bool(lease) and lease.get("held")
            and (time.time() - lease.get("at", 0)) < FLOOR_TTL)


def holder(stream: str, topic: str) -> dict | None:
    """Who holds the floor here, or None. Expired leases read as free."""
    lease = _read(stream, topic)
    return lease if _live(lease) else None


def backoff_for(share: float) -> float:
    """How long to wait before reaching for the floor, given your recent SHARE.

    Everyone pays jitter so three processes woken by one event do not race
    in lockstep. On top of that you wait in proportion to how much of the
    room's recent traffic was yours: hold none of the last HISTORY turns and
    you pay nothing, hold half of them and you pay half the penalty.

    Why share and not "did you just speak", which is the obvious version and
    what this had first. Measured over 60 contested rounds with a realistic
    judge-latency spread (Claude fastest, Kestrel +120ms, Yue +250ms):

        pure jitter            Claude 100%   Yue  0%   Kestrel  0%
        penalize last holder   Claude  50%   Yue  0%   Kestrel 50%
        penalize by share      roughly even

    Penalizing only the last holder is WORSE THAN USELESS for the slowest
    resident: the two fastest simply alternate, each clearing the other's
    penalty, and the third never gets in at all. Share-based backoff is what
    lets a consistently slower judge still win its turn — and Yue's judge is
    consistently slower, so this is her case specifically.

    Without any of it, the fastest judge wins every contested message
    forever. The penalty is the feature; the lock is just plumbing.
    """
    return random.uniform(0, BACKOFF_BASE) + RECENT_SPEAKER_PENALTY * share


def acquire(stream: str, topic: str, who: str,
            spoke_ago: float | None = None,
            trigger_id=None) -> tuple[bool, dict | None]:
    """Try to take the floor. Returns (got_it, current_holder_if_not).

    Sleeps a fairness backoff FIRST, then takes the lock — so the wait
    happens outside the critical section and never blocks another process.

    `spoke_ago` is optional and only an override: by default the penalty is
    derived from the lease file itself, which records who last held this
    floor. Deriving it from SHARED state rather than each process's private
    memory means it survives a restart and cannot disagree between
    residents — the same reason the game table exists.
    """
    if spoke_ago is None:
        prev = _read(stream, topic) or {}
        recent = prev.get("recent") or []
        mine = sum(1 for r in recent if _same(r, who))
        share = (mine / len(recent)) if recent else 0.0
    else:
        # Explicit override, kept for tests and for callers with a better
        # idea of their own recency than the room has.
        share = 1.0 if spoke_ago < 30 else 0.0
    time.sleep(backoff_for(share))
    with _locked(stream, topic):
        lease = _read(stream, topic)
        if _live(lease) and not _same(lease.get("who"), who):
            return False, lease
        recent = (lease or {}).get("recent") or []
        mine = {"who": who, "at": time.time(), "held": True,
                "trigger": trigger_id,
                "recent": (recent + [who])[-HISTORY:],
                "since": datetime.now(timezone.utc).isoformat()}
        _write(stream, topic, mine)
        return True, mine


def renew(stream: str, topic: str, who: str) -> bool:
    """Extend our lease. Called from the typing pulse; cheap and silent."""
    with _locked(stream, topic):
        lease = _read(stream, topic)
        if not lease or not _same(lease.get("who"), who):
            return False
        lease["at"] = time.time()
        _write(stream, topic, lease)
        return True


def release(stream: str, topic: str, who: str):
    """Give the floor back. Safe to call when we do not hold it."""
    with _locked(stream, topic):
        lease = _read(stream, topic)
        if lease and _same(lease.get("who"), who):
            # Keep the record, drop the claim: the next acquire reads this
            # to know we just had it and should yield.
            lease["held"] = False
            lease["at"] = time.time()
            _write(stream, topic, lease)
