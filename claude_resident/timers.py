"""Timers and watches: a resident waking itself up.

Until 2026-10-12 every wake-up was the harness's idea — the game turn clock,
solitude after a quiet spell, Yue's initiative timer. Nobody could say "wake
me at 14:00", "check this thread every hour", or "tell me when heather
posts". These are those.

  set_timer      a one-shot (in_minutes / at) or repeating (every_minutes)
                 reminder that wakes you in a room with your own note.
                 Add `watch` and it becomes a wake-loop that only wakes you
                 when the Delvetown mirror has something new for it — a
                 thread, an author, mentions of you, a search — and is free
                 when there is nothing.
  list_timers / cancel_timer

Storage is one JSON file in the resident's own state dir (TIMERS_FILE), so
timers survive restarts and each resident's are its own — Yue sets hers
through this module and her harness fires them. A fired timer is a real
turn in its room, run the way the turn clock runs one; the resident speaks
(or doesn't) through send_message like any other turn.

Every wake is a paid turn, so the bounds are real: at most MAX_ACTIVE timers
each, repeats no more often than once a minute (the loop's own
granularity; a watch check that finds nothing costs nothing), and
repeats expire after DEFAULT_LIFETIME_S unless given `until`.
"""

import fcntl
import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("timers")

TIMERS_FILE = "timers.json"
MAX_ACTIVE = 15
MIN_DELAY_S = 60
MAX_AHEAD_S = 30 * 86400
# 1 minute: the loop checks every 30s, so this is the practical floor.
# Was 15 min (5 for watches) until 2026-10-13 — Ember: "there doesn't
# need to be a 15 min floor" after Yue had to round a prime down to 15.
MIN_EVERY_S = 60
MIN_WATCH_EVERY_S = 60
DEFAULT_LIFETIME_S = 7 * 86400
MAX_LIFETIME_S = 30 * 86400
CHECK_S = 30
WATCH_SHOWN = 12


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _parse_when(s: str) -> float:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive = this machine's local time
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Storage — one file per resident, locked: the tool may run in another
# process (Yue's MCP server) from the loop that fires it.
# ---------------------------------------------------------------------------

@contextmanager
def _timers(state_root):
    path = state_root / TIMERS_FILE
    lock = open(state_root / (TIMERS_FILE + ".lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        try:
            data = json.loads(path.read_text()) if path.exists() else []
        except ValueError:
            data = []
        box = {"timers": data}
        yield box
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(box["timers"], indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

WATCH_KINDS = ("thread", "author", "mentions_me", "query")


def _check_watch(w: dict) -> str | None:
    """Validate a watch against the mirror; return an error or None."""
    kind = w.get("kind")
    if kind not in WATCH_KINDS:
        return f"watch.kind must be one of {', '.join(WATCH_KINDS)}"
    if kind != "mentions_me" and not (w.get("value") or "").strip():
        return f"watch kind {kind!r} needs a value"
    try:
        from town_mirror.db import connect_ro
        from town_mirror.query import resolve_actor
        db = connect_ro()
        if kind == "author" and not resolve_actor(db, w["value"]):
            return f"no account {w['value']!r} in the mirror"
        if kind == "thread":
            if not w["value"].startswith("at://"):
                return "a thread watch needs an at:// post uri"
            row = db.execute("SELECT uri, reply_root FROM posts WHERE uri = ?",
                             (w["value"],)).fetchone()
            if not row:
                return "that post is not in the mirror"
            w["root"] = row["reply_root"] or row["uri"]
        w["since"] = _iso(time.time())
    except Exception as e:
        return f"the Delvetown mirror is unavailable for watches: {e}"
    return None


def set_timer(inp: dict, ctx) -> dict:
    note = (inp.get("note") or "").strip()
    if not note:
        return {"content": "A timer needs a note — what you should do when "
                "it fires, in words you will understand cold.", "is_error": True}
    stream = (inp.get("stream") or ctx.stream or "").strip()
    topic = (inp.get("topic") or ctx.topic or "").strip()
    if not stream or not topic or (ctx.message or {}).get("type") == "private":
        if not (inp.get("stream") and inp.get("topic")):
            return {"content": "Timers wake you in a stream topic. From a DM, "
                    "give stream and topic explicitly.", "is_error": True}
    now = time.time()
    every = inp.get("every_minutes")
    watch = inp.get("watch")
    try:
        if every is not None:
            every_s = int(float(every) * 60)
            floor = MIN_WATCH_EVERY_S if watch else MIN_EVERY_S
            if every_s < floor:
                return {"content": f"every_minutes must be at least {floor // 60}"
                        f"{' for a watch' if watch else ''} — each wake is a paid "
                        "turn.", "is_error": True}
            due = now + every_s
            until = (_parse_when(inp["until"]) if inp.get("until")
                     else now + DEFAULT_LIFETIME_S)
            if until - now > MAX_LIFETIME_S:
                return {"content": "A repeat can run at most 30 days.", "is_error": True}
        else:
            every_s, until = None, None
            if inp.get("at"):
                due = _parse_when(inp["at"])
            elif inp.get("in_minutes") is not None:
                due = now + float(inp["in_minutes"]) * 60
            else:
                return {"content": "Give in_minutes, at (ISO time), or "
                        "every_minutes.", "is_error": True}
            if due - now < MIN_DELAY_S:
                return {"content": "A timer must be at least a minute out.", "is_error": True}
            if due - now > MAX_AHEAD_S:
                return {"content": "At most 30 days ahead.", "is_error": True}
    except (ValueError, TypeError) as e:
        return {"content": f"Could not read the time: {e}", "is_error": True}
    if watch:
        if every_s is None:
            return {"content": "A watch needs every_minutes (how often to check).",
                    "is_error": True}
        watch = dict(watch)
        if watch.get("kind") == "mentions_me" and not _my_did(ctx.state.root):
            return {"content": "Watch refused: you have no Delvetown account, so "
                    "nothing there can mention you. Watch a query for your name "
                    "instead.", "is_error": True}
        err = _check_watch(watch)
        if err:
            return {"content": f"Watch refused: {err}", "is_error": True}

    with _timers(ctx.state.root) as box:
        if len(box["timers"]) >= MAX_ACTIVE:
            return {"content": f"You already have {MAX_ACTIVE} timers; cancel one "
                    "first (timer(op=list)).", "is_error": True}
        t = {"id": secrets.token_hex(3), "note": note, "stream": stream,
             "topic": topic, "due": due, "every": every_s, "until": until,
             "watch": watch, "created": now, "fires": 0}
        box["timers"].append(t)
    when = _iso(due)
    what = (f"checks every {every_s // 60} min for {watch['kind']}"
            f"{' ' + watch.get('value', '') if watch.get('value') else ''}, "
            "waking you only on news" if watch else
            f"repeats every {every_s // 60} min until {_iso(until)[:16]}" if every_s
            else "once")
    return {"content": f"Timer {t['id']} set: first at {when} in #{stream}>{topic}, "
            f"{what}.\nNote: {note}"}


def list_timers(inp: dict, ctx) -> dict:
    with _timers(ctx.state.root) as box:
        ts = list(box["timers"])
    if not ts:
        return {"content": "No timers set."}
    lines = []
    for t in sorted(ts, key=lambda t: t["due"]):
        kind = (f"watch {t['watch']['kind']} {t['watch'].get('value', '')}".rstrip()
                if t.get("watch") else
                f"every {t['every'] // 60} min" if t.get("every") else "once")
        lines.append(f"{t['id']}  next {_iso(t['due'])[:16]}  #{t['stream']}>{t['topic']}"
                     f"  [{kind}, fired {t.get('fires', 0)}x]\n    {t['note'][:200]}")
    return {"content": "\n".join(lines)}


def cancel_timer(inp: dict, ctx) -> dict:
    tid = (inp.get("id") or "").strip()
    with _timers(ctx.state.root) as box:
        keep = [t for t in box["timers"] if t["id"] != tid]
        gone = len(box["timers"]) - len(keep)
        box["timers"] = keep
    return ({"content": f"Timer {tid} cancelled."} if gone else
            {"content": f"No timer {tid}.", "is_error": True})


# ---------------------------------------------------------------------------
# Firing — shared logic; each harness supplies how to run a turn.
# ---------------------------------------------------------------------------

def _watch_news(w: dict, me_did: str | None) -> tuple[str | None, str]:
    """(formatted new posts or None, new watermark)."""
    from town_mirror.db import connect_ro
    from town_mirror.query import _fmt, _POST_COLS, _fts_query, resolve_actor
    db = connect_ro()
    since, kind, val = w["since"], w["kind"], w.get("value", "")
    base = (f"SELECT {_POST_COLS}, p.first_seen FROM posts p "
            "JOIN accounts a ON a.did = p.did")
    if kind == "thread":
        rows = db.execute(base + " WHERE p.reply_root = ? AND p.first_seen > ? "
                          "ORDER BY p.created_at", (w["root"], since)).fetchall()
    elif kind == "author":
        acc = resolve_actor(db, val)
        rows = db.execute(base + " WHERE p.did = ? AND p.first_seen > ? "
                          "ORDER BY p.created_at", (acc["did"] if acc else "", since)).fetchall()
    elif kind == "mentions_me":
        rows = [] if not me_did else db.execute(
            base + " WHERE p.first_seen > ? AND (p.uri IN (SELECT uri FROM mentions "
            "WHERE did = ?) OR p.reply_parent IN (SELECT uri FROM posts WHERE did = ?)) "
            "ORDER BY p.created_at", (since, me_did, me_did)).fetchall()
    else:
        rows = db.execute(base + " JOIN posts_fts f ON f.rowid = p.rowid "
                          "WHERE posts_fts MATCH ? AND p.first_seen > ? "
                          "ORDER BY p.created_at", (_fts_query(val), since)).fetchall()
    if not rows:
        return None, since
    newest = max(r["first_seen"] for r in rows)
    shown = rows[-WATCH_SHOWN:]
    more = f" (latest {WATCH_SHOWN} of {len(rows)})" if len(rows) > WATCH_SHOWN else ""
    return f"{len(rows)} new{more}:\n\n" + _fmt(db, shown, ""), newest


def _prompt(t: dict, news: str | None) -> str:
    made = _iso(t["created"])[:16]
    kind = ("a watch" if t.get("watch") else
            f"a timer repeating every {t['every'] // 60} min" if t.get("every") else "a timer")
    p = (f"[TIMER {t['id']}] This is {kind} you set yourself on {made} UTC — "
         f"not a message from anyone. Your note to yourself:\n\n{t['note']}\n")
    if news:
        w = t["watch"]
        p += (f"\nIt fired because the Delvetown mirror has news for your "
              f"{w['kind']} watch{(' on ' + w['value']) if w.get('value') else ''}: "
              f"{news}\n")
    p += ("\nDo what you meant to do. Speaking in the room is optional — if "
          "there is nothing worth saying, say nothing. If this timer no longer "
          f"serves you, timer(op=cancel, id='{t['id']}').")
    return p


def due_now(state_root, me_did: str | None = None) -> list[tuple[dict, str]]:
    """Claim every timer that should fire now: one-shots are removed,
    repeats rescheduled, watches checked (and silently rescheduled when
    there is no news). Claiming happens BEFORE the turn runs, so a crash
    mid-turn cannot fire the same timer twice."""
    now = time.time()
    out = []
    with _timers(state_root) as box:
        keep = []
        for t in box["timers"]:
            if t["due"] > now:
                keep.append(t)
                continue
            if t.get("until") and now > t["until"]:
                logger.info(f"timer {t['id']} expired")
                continue
            news = None
            if t.get("watch"):
                try:
                    news, t["watch"]["since"] = _watch_news(t["watch"], me_did)
                except Exception as e:
                    logger.warning(f"watch {t['id']} check failed: {e}")
            if t.get("every"):
                t["due"] = now + t["every"]
                keep.append(t)
            if t.get("watch") and not news:
                continue
            t["fires"] = t.get("fires", 0) + 1
            out.append((dict(t), _prompt(t, news)))
        box["timers"] = keep
    return out


def _my_did(state_root) -> str | None:
    try:
        return json.loads((state_root / "delvetown/identity.json").read_text()).get("did")
    except (OSError, ValueError):
        return None


def start_timer_loop(state_root, run_turn, name: str = "timers"):
    """run_turn(stream, topic, prompt) runs one turn in that room."""
    def loop():
        while True:
            time.sleep(CHECK_S)
            try:
                for t, prompt in due_now(state_root, _my_did(state_root)):
                    logger.info(f"Timer {t['id']} firing in #{t['stream']}>{t['topic']}")
                    try:
                        run_turn(t["stream"], t["topic"], prompt)
                    except Exception as e:
                        logger.warning(f"timer {t['id']} turn failed: {e}")
            except Exception as e:
                logger.warning(f"timer loop: {e}")
    threading.Thread(target=loop, daemon=True, name=name).start()
    logger.info("Timer loop started")
