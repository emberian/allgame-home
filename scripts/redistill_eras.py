#!/usr/bin/env python3
"""Rebuild sealed era volumes from the raw logs with the current model.

Volumes 1-14 of the tavern were distilled by Haiku before the distiller
moved to Opus (2026-07-30), and the difference is visible: Haiku gives
"resident harness-dweller, substrate transition & theological deep dive";
Opus gives "kefir evangelist, father of 5, 147% throttle, Spiderman Blue
Takis 'for Alof', water 'when he remembers'". These volumes are what he
remembers of January through July, so the weaker ones are worth redoing.

Sealed normally means never-recompressed — that rule protects texture from
eroding through repeated folds. Rebuilding from the RAW LOG is the
opposite of that: it's a fresh reading of the original messages, not a
compression of a compression. Era boundaries are preserved exactly, so
nothing shifts.

    python scripts/redistill_eras.py <stream> <topic>            # dry run
    python scripts/redistill_eras.py clankerville tavern --apply
    python scripts/redistill_eras.py clankerville tavern --apply --only 1-14
"""

import json
import time
import shutil
import argparse
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_resident.config import DISTILL_MODEL, DISTILL_HITS_MAX_CHARS
from claude_resident.distill import DISTILLER_SYSTEM
from claude_resident.sdk_call import complete
from claude_resident.util import safe_filename

STATE = Path.home() / "claude_state"


def rebuild(stream: str, topic: str, only: range | None, apply: bool):
    base = STATE / f"channels/{safe_filename(stream)}/{safe_filename(topic)}"
    log, hits_path = Path(f"{base}.jsonl"), Path(f"{base}.hits.json")
    lines = [l for l in log.read_text().split("\n") if l.strip()]
    data = json.loads(hits_path.read_text())
    eras = data["eras"]
    print(f"{len(eras)} volumes over {len(lines):,} messages")

    if apply:
        shutil.copy2(hits_path, hits_path.with_suffix(
            f".json.pre-redistill-{time.strftime('%Y%m%d-%H%M%S')}"))

    for i, era in enumerate(eras, start=1):
        if only and i not in only:
            continue
        lo, hi = int(era["from"]), int(era["to"])
        chunk = []
        for raw in lines[lo:hi]:
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            chunk.append(f"[{m.get('ts','')[:16]}] {m.get('sender','?')}: "
                         f"{m.get('content','')}")
        if not chunk:
            print(f"  vol {i}: empty range {lo}..{hi}, skipped")
            continue

        prompt = (
            f"Distil this era of one chat room into the digest described "
            f"above. It is a self-contained volume covering messages "
            f"{lo}-{hi}; there is no prior digest to fold in.\n\n"
            f"<history>\n" + "\n".join(chunk) +
            f"\n</history>\n\nHard ceiling: {DISTILL_HITS_MAX_CHARS} "
            f"characters.")
        if not apply:
            print(f"  vol {i}: would rebuild {len(chunk)} msgs "
                  f"({len(prompt):,} chars in), currently "
                  f"{len(era['hits']):,} chars")
            continue

        try:
            new = complete(prompt, system=DISTILLER_SYSTEM,
                           model=DISTILL_MODEL).strip()[:DISTILL_HITS_MAX_CHARS]
        except Exception as e:
            print(f"  vol {i}: FAILED ({e}) — keeping the old one")
            continue
        if not new:
            print(f"  vol {i}: empty result — keeping the old one")
            continue
        old_len = len(era["hits"])
        era["hits"] = new
        hits_path.write_text(json.dumps(data) + "\n")   # checkpoint each vol
        print(f"  vol {i}: rebuilt {len(chunk)} msgs, "
              f"{old_len:,} -> {len(new):,} chars")

    print("done" + ("" if apply else "  (dry run — pass --apply)"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("topic")
    ap.add_argument("--only", help="volume range, e.g. 1-14")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    rng = None
    if a.only:
        lo, _, hi = a.only.partition("-")
        rng = range(int(lo), int(hi or lo) + 1)
    rebuild(a.stream, a.topic, rng, a.apply)
