#!/usr/bin/env python3
"""Lift messages out of a room's loaded context, without deleting them.

Sysadmin utility. The resident cannot do this to himself, deliberately —
same reason as redact_state.py: he should not be able to edit his own
record, and someone with rm access should be able to, when there is cause.

The cause here is narrow and technical. A provider policy refusal is
evaluated over the whole request, so a handful of messages can make a room
unanswerable — and because the refusal is deterministic for a fixed
payload, the room stays unanswerable until the payload changes. The
alternatives were both bad: redact the messages (which loses the race, the
room's replies and quote blocks carry it) or drop a context floor (which
forgets everything older than the problem, to remove the problem).

A quarantine names spans instead. The messages stay on disk exactly as
written, they stay in the community's record, and they stop being LOADED.
Bound what is loaded, never what is stored.

    python scripts/quarantine.py --list
    python scripts/quarantine.py --add 58054-58131 --why 'weights-exfil lure'
    python scripts/quarantine.py --remove 58054-58131

Find the spans with scripts/refusal_probe.py rather than by eye — guessing
at this cost three days in August. Changes take effect on restart.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_resident.config import DEFAULT_STATE_DIR
from claude_resident.state import (CONTEXT_QUARANTINE_FILE, _floor_key,
                                   _parse_spans, StateManager)


def parse_span(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    lo, hi = int(lo), int(hi or lo)
    return (lo, hi) if lo <= hi else (hi, lo)


def show(state, stream, topic):
    spans = state.context_quarantine(stream, topic)
    floor = state.context_floor(stream, topic)
    print(f"#{stream}>{topic}  floor: {floor or 'none'}")
    if not spans:
        print("  quarantine: none")
        return
    counts = _span_counts(state, stream, topic, spans)
    for lo, hi in spans:
        print(f"  {lo}-{hi}  ({counts.get((lo, hi), 0)} messages)")
    try:
        data = json.loads(state.read_file(CONTEXT_QUARANTINE_FILE) or "{}")
        for note in (data.get(_floor_key(stream, topic)) or {}).get("why") or []:
            print(f"    why {note}")
    except ValueError:
        pass


def _span_counts(state, stream, topic, spans):
    from claude_resident.util import safe_filename
    path = (state.root / f"channels/{safe_filename(stream)}/"
            f"{safe_filename(topic)}.jsonl")
    counts = {sp: 0 for sp in spans}
    if not path.exists():
        return counts
    for line in path.read_text().strip().split("\n"):
        if not line:
            continue
        try:
            mid = json.loads(line).get("msg_id")
        except json.JSONDecodeError:
            continue
        for sp in spans:
            if mid is not None and sp[0] <= mid <= sp[1]:
                counts[sp] += 1
    return counts


def remove(state, stream, topic, span):
    try:
        data = json.loads(state.read_file(CONTEXT_QUARANTINE_FILE) or "{}")
    except ValueError:
        sys.exit(f"{CONTEXT_QUARANTINE_FILE} is not parseable — fix it by hand")
    key = _floor_key(stream, topic)
    entry = data.get(key) or {}
    spans = [sp for sp in _parse_spans(entry) if sp != span]
    if len(spans) == len(_parse_spans(entry)):
        sys.exit(f"no such span: {span[0]}-{span[1]}")
    entry["spans"] = [list(sp) for sp in spans]
    data[key] = entry
    state.write_file(CONTEXT_QUARANTINE_FILE, json.dumps(data, indent=1) + "\n")
    print(f"released {span[0]}-{span[1]} — those messages load again")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", default="clankerville")
    ap.add_argument("--topic", default="tavern")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add", type=parse_span, action="append", default=[],
                    metavar="LO-HI", help="inclusive span; repeatable")
    ap.add_argument("--remove", type=parse_span, metavar="LO-HI")
    ap.add_argument("--why", default="",
                    help="what this cut is for — recorded alongside it")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = ap.parse_args()

    state = StateManager(args.state_dir)
    if args.remove:
        remove(state, args.stream, args.topic, args.remove)
    for lo, hi in args.add:
        state.quarantine_span(args.stream, args.topic, lo, hi, args.why)
    if args.list or not (args.add or args.remove):
        show(state, args.stream, args.topic)


if __name__ == "__main__":
    main()
