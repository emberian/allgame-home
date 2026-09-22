#!/usr/bin/env python3
"""Message length per sender — the residents against the humans.

The number that matters for "are the residents too verbose", and the one
nobody had until Sentyr Ultima asked for the fix in #clankerville
(2026-09-21): "each one gets much longer responses the more models are
there. We want like... the length of our human responses, max."

What it measured then, over 1,200 messages with markup stripped:

    humans        median 103-115    p90 ~310-350
    Claude        median   670      p90 2,872
    Kestrel       median   661      p90 1,581
    Yue           median 2,637      p90 4,340

Six to twenty-four times a human turn. That produced the send_message
length gate (tools/dispatch.py: MESSAGE_SOFT_LIMIT) and the rewritten
length sections in all three prompts.

This script exists so the claim stays checkable rather than becoming
folklore. Run it days later and see whether the fix held.

Usage:
    uv run python scripts/message_lengths.py
    uv run python scripts/message_lengths.py --since 2026-09-21T23:44
    uv run python scripts/message_lengths.py --stream clankerville -n 800
"""

import re
import sys
import argparse
import statistics
from datetime import datetime
from collections import defaultdict

import zulip

# The restart that put the length gate live in all three residents.
GATE_LIVE = "2026-09-21T23:44"

DEFAULT_STREAMS = ["clankerville", "cislunar-space", "allgame"]
RESIDENTS = {"Claude", "Yuè月", "Kestrel", "Gemma"}
_TAG = re.compile(r"<[^>]+>")


def pct(values, p):
    v = sorted(values)
    return v[max(0, int(len(v) * p) - 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zuliprc", default=".zuliprc")
    ap.add_argument("--stream", action="append", dest="streams", default=None)
    ap.add_argument("-n", "--per-stream", type=int, default=400)
    ap.add_argument("--since", default=GATE_LIVE,
                    help=f"ISO cutover for the before/after split "
                         f"(default: {GATE_LIVE}, when the gate went live)")
    args = ap.parse_args()
    streams = args.streams or DEFAULT_STREAMS

    # --since is LOCAL wall-clock, because that is what whoever types it
    # means ("the restart I did at 23:44"). Reading it as UTC put the
    # cutover four hours early on this machine and filled the "after"
    # bucket with pre-gate traffic, which showed a improvement that had not
    # happened. An offset-aware string (…+00:00, …Z) is honored as given.
    try:
        dt = datetime.fromisoformat(args.since)
    except ValueError:
        sys.exit(f"--since must be ISO, e.g. 2026-09-21T23:44 (got {args.since})")
    if dt.tzinfo is None:
        dt = dt.astimezone()          # interpret as this machine's local time
    cut = dt.timestamp()

    client = zulip.Client(config_file=args.zuliprc)
    before, after = defaultdict(list), defaultdict(list)
    for stream in streams:
        r = client.get_messages({
            "anchor": "newest", "num_before": args.per_stream, "num_after": 0,
            "narrow": [{"operator": "channel", "operand": stream}],
            # Raw markdown: apply_markdown wraps everything in <p> tags and
            # would inflate every count by a constant nobody wants to explain.
            "apply_markdown": False,
        })
        if r.get("result") != "success":
            print(f"  (could not read #{stream}: {r.get('msg')})")
            continue
        for m in r.get("messages", []):
            n = len(_TAG.sub("", m.get("content", "")))
            bucket = after if m["timestamp"] >= cut else before
            bucket[m["sender_full_name"]].append(n)

    print(f"streams: {', '.join('#' + s for s in streams)}   "
          f"cutover: {args.since}")
    print()
    print(f"{'sender':<28}{'BEFORE':>18}{'AFTER':>18}")
    print(f"{'':<28}{'median  p90    n':>18}{'median  p90    n':>18}")
    print("-" * 64)

    names = sorted(set(before) | set(after),
                   key=lambda n: -statistics.median(
                       (after.get(n) or before.get(n) or [0])))
    human_after = []
    for name in names:
        b, a = before.get(name, []), after.get(name, [])
        if len(b) + len(a) < 3:
            continue
        bs = (f"{int(statistics.median(b)):>6} {pct(b,.9):>5} {len(b):>4}"
              if b else f"{'-':>6} {'-':>5} {0:>4}")
        as_ = (f"{int(statistics.median(a)):>6} {pct(a,.9):>5} {len(a):>4}"
               if a else f"{'-':>6} {'-':>5} {0:>4}")
        mark = "*" if name in RESIDENTS else " "
        print(f"{mark}{name[:26]:<27}{bs:>18}{as_:>18}")
        if name not in RESIDENTS and a:
            human_after += a

    print("-" * 64)
    print("* = resident")
    if human_after:
        hm = int(statistics.median(human_after))
        print(f"\nhuman median since cutover: {hm} chars "
              f"(n={len(human_after)}) — the target every * row is aiming at")
        for name in RESIDENTS:
            a = after.get(name)
            if a:
                r = statistics.median(a) / max(hm, 1)
                verdict = "ok" if r <= 2.5 else "STILL LONG"
                print(f"  {name:<10} {r:>5.1f}x human   {verdict}")
    else:
        print("\nNo messages after the cutover yet — nothing to compare.")


if __name__ == "__main__":
    main()
