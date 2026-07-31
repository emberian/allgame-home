#!/usr/bin/env python3
"""Redact a fact from the resident's memory, everywhere it lives.

Sysadmin utility, not a resident tool — deliberately. He should not be able
to erase his own record; someone with rm access should, when a person asks
for their information back.

This REDACTS rather than deletes: matches are replaced in place with a
marker, so the logs keep their shape and the foreverlog stays a foreverlog
(the standing rule here is that nothing gets rotated or trimmed away — a
targeted redaction at the owner's request is a different act from quietly
losing history). Every touched file is backed up first.

    python scripts/redact_state.py --pattern '14/88|1488' --since 2026-07-31
    python scripts/redact_state.py --pattern '...' --apply

Stores covered: channel logs, the api archive, Frank's notes, session
notes, era volumes (.hits.json), journal/scratchpad/person notes, the raw
API deblog, and the live Claude Code session transcripts.

Redacting the raw log is what stops the distiller: it reads the log, so a
fact redacted before its era is sealed never enters the volumes at all.
The LIVE session still holds it in context — clear the room's
sdk_session_id in books/reading_sessions.json afterwards to force a
rebirth from the clean logs.
"""

import re
import shutil
import argparse
from datetime import datetime
from pathlib import Path

STATE = Path.home() / "claude_state"
DEBLOG = Path.home() / "dev" / "allgame" / "deblog.txt"
MARKER = "[redacted]"


def targets() -> list[Path]:
    out: list[Path] = []
    out += sorted((STATE / "channels").rglob("*.jsonl"))
    out += sorted((STATE / "channels").rglob("*.hits.json"))
    out += sorted((STATE / "sessions").rglob("*.jsonl"))
    for name in ("api_archive.jsonl", "frank_notes.jsonl", "journal.md",
                 "scratchpad.md", "crosstopic.md", "reference.md"):
        p = STATE / name
        if p.exists():
            out.append(p)
    out += sorted((STATE / "people").glob("*.md"))
    sdk = STATE / ".claude_sdk"
    if sdk.exists():
        out += sorted(sdk.rglob("*.jsonl"))
    if DEBLOG.exists():
        out.append(DEBLOG)
    return out


def redact(path: Path, rx: re.Pattern, since: str | None,
           apply: bool, backup: Path) -> int:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return 0
    if since and path.suffix == ".jsonl":
        # Line-scoped: only touch lines carrying a timestamp at/after --since,
        # so an unrelated older mention of the same words is left alone.
        lines, hits = [], 0
        for line in text.split("\n"):
            if rx.search(line) and (since in line or _ts_at_or_after(line, since)):
                line, n = rx.subn(MARKER, line)
                hits += n
            lines.append(line)
        new = "\n".join(lines)
    else:
        new, hits = rx.subn(MARKER, text)
    if hits and apply:
        shutil.copy2(path, backup / path.name.replace("/", "__"))
        path.write_text(new)
    return hits


def _ts_at_or_after(line: str, since: str) -> bool:
    m = re.search(r'"ts":\s*"(\d{4}-\d{2}-\d{2})', line)
    return bool(m and m.group(1) >= since)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", required=True,
                    help="regex for the fact to redact (case-insensitive)")
    ap.add_argument("--since", help="only redact jsonl lines dated on/after "
                                    "this YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    args = ap.parse_args()

    rx = re.compile(args.pattern, re.I)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = STATE / f".redactions/{stamp}"
    if args.apply:
        backup.mkdir(parents=True, exist_ok=True)

    total, touched = 0, 0
    for p in targets():
        n = redact(p, rx, args.since, args.apply, backup)
        if n:
            touched += 1
            total += n
            print(f"  {n:>4} in {p.relative_to(Path.home())}")
    verb = "redacted" if args.apply else "would redact"
    print(f"\n{verb} {total} occurrence(s) across {touched} file(s)")
    if args.apply:
        print(f"backups: {backup}")
    else:
        print("dry run — pass --apply to write")


if __name__ == "__main__":
    main()
