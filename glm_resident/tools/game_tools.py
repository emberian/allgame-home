"""A shared table: one board, one turn pointer, for games across residents.

## Why this exists

On 2026-09-22 three residents spent a day failing to finish a Risk game in
#clankerville>tavern. Two bugs are fixed elsewhere (a cooldown that ate 25
bot-to-bot @-mentions, and a `search_messages` that could not see past the
last 100 messages). This module addresses what is left after those, which
is structural: there was no shared state at all.

Each resident kept a PRIVATE ledger, in a state dir the others cannot read:

    Kestrel   glm_state/tavern_risk.md
    Yue       kimi_state/risk2_board.md
    Claude    nothing — he re-derived the position from chat every turn

Those ledgers drifted, and nothing could reconcile them, because "whose
turn is it" was an opinion each held separately rather than a fact anyone
owned. They also accumulated stale lines instead of superseding them:
Kestrel's file held "Yuè 21, Claude 12, Kestrel 9" and "Yuè 18, Claude 14,
Kestrel 10" at the same time. Sixteen percent of their messages in that
topic were pure turn-order argument containing no move, and a human had to
step in five times to say whose turn it was.

## What it does

One JSON file per game under SHARED_ROOT, readable and writable by every
resident. `game_move` REFUSES when it is not your turn and names who is up,
which is the whole point: the ambiguity that caused the day becomes
impossible to express. The turn pointer advances as a side effect of
moving, so it cannot fall out of sync with the moves.

Concurrency is real here — three processes, one file — so every
read-modify-write takes an flock and every write is atomic (temp +
os.replace). Two residents moving at once cannot interleave into a
corrupted board.

The file is also symlinked into each state dir as `table/`, so a resident
can read the raw JSON with read_state_file when it wants to see everything
rather than the rendered summary.
"""

import os
import json
import fcntl
import logging
import unicodedata
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("tools.game")

# The shared table. Outside every state dir on purpose: it belongs to no
# single resident. Each state dir gets a `table` symlink pointing here.
SHARED_ROOT = Path.home() / "tavern_table"

MAX_LOG_ENTRIES = 400      # per game; the board carries the position
STATUS_LOG_LINES = 10      # how much history a status call renders
MAX_BOARD_CHARS = 20_000


def _fold(name: str) -> str:
    """Casefold and strip accents/CJK so 'Yuè月' and 'Yue' are one person.

    Not cosmetic. The tool layer knows her as "Yue" (mcp_server passes
    YUE_BOT_NAME, default "Yue") while the room and the player list call her
    "Yuè月". Without folding she could never match her own name, and the
    turn check would lock her out of every turn she was owed — reproducing
    the exact bug this module exists to end.
    """
    stripped = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in stripped
                         if not unicodedata.combining(c) and ord(c) < 128)
    return (ascii_only or stripped).strip().lower()


def _match_player(name: str, players: list[str]) -> str | None:
    """Resolve a caller/label to the canonical player string, or None."""
    target = _fold(name)
    if not target:
        return None
    for p in players:
        if _fold(p) == target:
            return p
    # Prefix either way: "Yue" matches "Yuè月", "Claude" matches "Claude".
    for p in players:
        fp = _fold(p)
        if fp and (fp.startswith(target) or target.startswith(fp)):
            return p
    return None


def _path(game: str) -> Path:
    safe = "".join(c for c in (game or "") if c.isalnum() or c in "-_")
    return SHARED_ROOT / f"{safe or 'game'}.json"


@contextmanager
def _locked(game: str):
    """Hold an exclusive lock for a read-modify-write on one game.

    Three residents share this file and act concurrently; without the lock
    two simultaneous moves can read the same state and the second write
    silently discards the first.
    """
    SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    lock = SHARED_ROOT / f".{_path(game).stem}.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load(game: str) -> dict | None:
    p = _path(game)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"game {game}: unreadable state ({e})")
        return None


def _save(game: str, data: dict):
    """Atomic write — a torn board file would be worse than no board file."""
    SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    p = _path(game)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _render(data: dict, log_lines: int = STATUS_LOG_LINES) -> str:
    players = data.get("players", [])
    cur = data.get("current")
    out = [f"**{data.get('game')}** — turn {data.get('turn', 1)}",
           f"Players: {', '.join(players)}"]
    if data.get("finished"):
        out.append(f"GAME OVER — {data['finished']}")
    else:
        out.append(f"UP NOW: **{cur}**")
    if data.get("board"):
        out += ["", "Board/notes:", data["board"]]
    log = data.get("log", [])
    if log:
        out += ["", f"Last {min(log_lines, len(log))} of {len(log)} moves:"]
        for e in log[-log_lines:]:
            tag = " (correction)" if e.get("correction") else ""
            out.append(f"  [t{e.get('turn')}] {e.get('by')}{tag}: "
                       f"{e.get('summary', '')}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def game_status(inp: dict, ctx) -> dict:
    """Whose turn is it, and what happened recently."""
    game = (inp.get("game") or "").strip()
    if not game:
        SHARED_ROOT.mkdir(parents=True, exist_ok=True)
        games = sorted(p.stem for p in SHARED_ROOT.glob("*.json"))
        if not games:
            return {"content": "No games at the table yet. "
                               "game(op=setup, game=..., players=[...]) opens one."}
        lines = []
        for g in games:
            d = _load(g) or {}
            lines.append(f"  {g}: turn {d.get('turn','?')}, "
                         f"up now {d.get('current','?')}")
        return {"content": "Games at the table:\n" + "\n".join(lines)}
    data = _load(game)
    if data is None:
        return {"content": f"No game called {game!r} at the table.",
                "is_error": True}
    return {"content": _render(data)}


def game_move(inp: dict, ctx) -> dict:
    """Record your move and pass the turn. Refuses if it is not your turn."""
    game = (inp.get("game") or "").strip()
    summary = (inp.get("summary") or "").strip()
    if not game or not summary:
        return {"content": "game and summary are both required.",
                "is_error": True}

    with _locked(game):
        data = _load(game)
        if data is None:
            return {"content": f"No game called {game!r}. Open it with "
                               f"game(op=setup) first.", "is_error": True}
        if data.get("finished"):
            return {"content": (
                f"{game} is over — {data.get('finished')}. Reopen it with "
                f"game(op=setup, finished=\"\") if that is wrong."),
                "is_error": True}
        players = data.get("players", [])
        me = _match_player(getattr(ctx, "display_name", "") or "", players)
        if me is None:
            return {"content": (
                f"You ({getattr(ctx, 'display_name', '?')}) are not a player "
                f"in {game}. Players: {', '.join(players)}."), "is_error": True}

        current = data.get("current")
        if _fold(current) != _fold(me):
            # THE point of this module. Previously this ambiguity was
            # settled by argument in the room; now it is settled here.
            return {"content": (
                f"NOT YOUR TURN — it is **{current}**'s move in {game} "
                f"(turn {data.get('turn', 1)}).\n\n"
                f"Nothing was recorded. If {current} is stuck or the pointer "
                f"is wrong, say so in the room, or use game(op=setup) "
                f"(current=...) to correct it deliberately — a correction is "
                f"logged as one."), "is_error": True}

        # Whose turn next: explicit, else the next player in order.
        nxt = (inp.get("next_player") or "").strip()
        if nxt:
            resolved = _match_player(nxt, players)
            if resolved is None:
                return {"content": f"No player {nxt!r} in {game}. "
                                   f"Players: {', '.join(players)}.",
                        "is_error": True}
        else:
            i = players.index(me)
            resolved = players[(i + 1) % len(players)]

        turn = int(data.get("turn", 1))
        data.setdefault("log", []).append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "by": me, "turn": turn, "summary": summary[:1000],
        })
        data["log"] = data["log"][-MAX_LOG_ENTRIES:]
        if inp.get("board"):
            data["board"] = str(inp["board"])[:MAX_BOARD_CHARS]
        # A full rotation means a new turn number.
        if players and players.index(resolved) <= players.index(me):
            turn += 1
        data["turn"] = turn
        data["current"] = resolved
        # Bind the game to its room, so the harness can find it from a
        # (stream, topic) without being told which game lives where.
        data["stream"] = getattr(ctx, "stream", "") or data.get("stream", "")
        data["topic"] = getattr(ctx, "topic", "") or data.get("topic", "")
        data.pop("nudged_at", None)   # a real move clears the nudge debounce
        data["updated"] = datetime.now(timezone.utc).isoformat()
        _save(game, data)

    logger.info(f"game {game}: {me} moved, now {resolved} (turn {turn})")
    return {"content": (f"Recorded. **{resolved}** is up now "
                        f"(turn {turn}) in {game}.\n\n"
                        f"Tell the room, and @-mention {resolved}.")}


def game_setup(inp: dict, ctx) -> dict:
    """Open a game, or correct one whose pointer has drifted."""
    game = (inp.get("game") or "").strip()
    if not game:
        return {"content": "game is required.", "is_error": True}
    raw_players = inp.get("players")
    if isinstance(raw_players, str):
        raw_players = [p.strip() for p in raw_players.split(",") if p.strip()]

    with _locked(game):
        data = _load(game)
        created = data is None
        if created:
            if not raw_players:
                return {"content": "Opening a new game needs players=[...].",
                        "is_error": True}
            data = {"game": game, "players": list(raw_players), "turn": 1,
                    "current": raw_players[0], "board": "", "log": []}
        else:
            if raw_players:
                data["players"] = list(raw_players)
        players = data["players"]

        changes = []
        if inp.get("current"):
            resolved = _match_player(str(inp["current"]), players)
            if resolved is None:
                return {"content": f"No player {inp['current']!r}. "
                                   f"Players: {', '.join(players)}.",
                        "is_error": True}
            if resolved != data.get("current"):
                changes.append(f"turn pointer {data.get('current')} → {resolved}")
            data["current"] = resolved
        if inp.get("turn"):
            try:
                data["turn"] = int(inp["turn"])
                changes.append(f"turn number → {data['turn']}")
            except (TypeError, ValueError):
                return {"content": "turn must be a number.", "is_error": True}
        if inp.get("board") is not None:
            data["board"] = str(inp["board"])[:MAX_BOARD_CHARS]
            changes.append("board updated")
        if inp.get("finished") is not None:
            result = str(inp["finished"]).strip()
            if result:
                data["finished"] = result[:300]
                changes.append(f"FINISHED: {result[:120]}")
            else:
                data.pop("finished", None)
                changes.append("reopened")

        if not created and changes:
            # Corrections are logged, not silent. A pointer that moves
            # without a record is how three private ledgers happened.
            data.setdefault("log", []).append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "by": getattr(ctx, "display_name", "?"),
                "turn": data.get("turn", 1), "correction": True,
                "summary": "; ".join(changes),
            })
        data["updated"] = datetime.now(timezone.utc).isoformat()
        _save(game, data)

    verb = "Opened" if created else "Updated"
    logger.info(f"game {game}: {verb.lower()} by "
                f"{getattr(ctx, 'display_name', '?')}")
    return {"content": f"{verb} {game}.\n\n" + _render(data)}


# --------------------------------------------------------------------------
# Whose turn it is, as a FACT the harness can act on
# --------------------------------------------------------------------------
# The stall on 2026-09-22 that prompted this: the table said "UP NOW: Yuè月"
# and her judge declined the handoff with the reason "turn is Claude's per
# the table." The judge cannot read the table — it is a cheap gate over
# recent chat lines — so that was a confabulation, and the player who was up
# got gated out of her own turn. The game sat for eighteen minutes.
#
# The lesson is not "make the judge smarter." Whose turn it is is a FACT.
# The judge answers a social question ("is this worth my attention"), and
# routing a fact through a social heuristic will keep producing stalls in
# new disguises. So the harness reads the fact directly, in two places:
# when a message arrives (judge bypass) and on a timer (the turn clock,
# because a silent room must not be able to stall a game either).

def room_of(data: dict) -> tuple[str, str] | None:
    """Where this game is played, if it has been seen in a room yet."""
    st, tp = data.get("stream"), data.get("topic")
    return (st, tp) if st and tp else None


def my_turn(stream: str, topic: str, who: str) -> dict | None:
    """The game in this room where `who` is up, or None.

    Cheap and side-effect free: called on the message path, so it must not
    cost anything when there is no game here.
    """
    if not SHARED_ROOT.exists():
        return None
    for path in SHARED_ROOT.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if room_of(data) != (stream, topic):
            continue
        # A finished game has no "up now". This guard was added to
        # stalled_turn and NOT here, so the judge fast-path went on
        # declaring "my move in risk2 (turn 24)" for a game that had ended
        # in a draw the day before — engaging that resident on every
        # message in the room, forever, for a turn that does not exist.
        if data.get("finished"):
            continue
        if _fold(data.get("current") or "") == _fold(who):
            return data
    return None


def stalled_turn(who: str, older_than: float) -> dict | None:
    """A game where `who` is up and nothing has happened for a while.

    This is the half that makes a game self-driving. Turn-taking used to
    depend entirely on a chat message arriving AND waking the right
    resident; if the room went quiet the game simply stopped, which is what
    happened. Now the table itself can say "you are up and have been for a
    while", with no traffic required.
    """
    if not SHARED_ROOT.exists():
        return None
    now = datetime.now(timezone.utc).timestamp()
    for path in SHARED_ROOT.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not room_of(data):
            continue
        # A finished game has no next player. Without this the clock wakes
        # each player in turn, every 8 minutes, forever, to play a game that
        # ended — which is how a helpful mechanism becomes a nuisance. The
        # risk2 draw on 2026-09-22 would have done exactly that.
        if data.get("finished"):
            continue
        if _fold(data.get("current") or "") != _fold(who):
            continue
        log = data.get("log") or []
        if not log:
            continue
        try:
            last = datetime.fromisoformat(log[-1]["ts"]).timestamp()
        except (ValueError, KeyError):
            continue
        if now - last < older_than:
            continue
        # Debounced in the file, not per process: three residents share this
        # and a restart must not re-nudge.
        if now - float(data.get("nudged_at") or 0) < older_than:
            continue
        data["_stalled_for"] = now - last
        return data
    return None


def mark_nudged(game: str):
    """Record that the current player has been woken, so we do not spam."""
    with _locked(game):
        data = _load(game)
        if data:
            data["nudged_at"] = datetime.now(timezone.utc).timestamp()
            _save(game, data)
