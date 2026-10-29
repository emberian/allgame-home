#!/usr/bin/env python3
"""Have a resident's own model rewrite files that are in its own voice.

Ember, 2026-10-25: a resident's own files — identity, scratchpad, people
notes, session notes — should hold only that resident's own model's
writing. When a review (a person, or another model) finds errors in them,
the fix goes back through the resident's model: it gets its own version,
the reviewed version and the reviewer's notes, and writes the file again in
its own words. The reviewer's facts survive; the reviewer's prose doesn't.

    python scripts/selfwrite.py kestrel files identity.md people/yue.md   # writes to review/
    python scripts/selfwrite.py claude notes                              # newest session notes
    python scripts/selfwrite.py kestrel files identity.md --apply         # install from review/

Results go to <state>/.ember_edits/<date>-selfwrite/review/ first; --apply
copies them over the live files (the live version is kept beside them as
<name>.before-selfwrite). Session notes keep the old note in the row as
"summary_prev" — the sessions/ logs are history, so nothing is dropped.
"""

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
DATE = "2026-10-25"

RESIDENTS = {
    "claude": {"state": Path.home() / "claude_state", "name": "Claude", "pkg": "claude_resident"},
    "kestrel": {"state": Path.home() / "glm_state", "name": "Kestrel", "pkg": "glm_resident"},
    # Yue's existing files were written by K3, before her swap to K3.1; K3
    # rewrites them so her preregistered before-baseline stays K3's words.
    "yue": {"state": Path.home() / "kimi_state", "name": "Yuè月", "model": "k3"},
}


# ---- calling the resident's own model ------------------------------------------

def call(who: str, system: str, prompt: str) -> str:
    import time
    for attempt in range(4):
        try:
            return _call(who, system, prompt)
        except Exception as e:
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} after: {str(e)[:120]}", flush=True)
            time.sleep(20 * (attempt + 1))


def _call(who: str, system: str, prompt: str) -> str:
    if who == "claude":
        from claude_resident.sdk_call import complete
        return complete(prompt, system=system, model="claude-opus-4-6", big_context=True).strip()
    if who == "kestrel":
        from glm_resident.sdk_call import complete
        from glm_resident.config import AGENT_SDK_MODEL
        return complete(prompt, system=system, model=AGENT_SDK_MODEL).strip()
    if who == "yue":
        import requests
        key = (Path.home() / ".kimikey").read_text().strip()
        r = requests.post("https://api.kimi.com/coding/v1/messages", timeout=900,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": RESIDENTS["yue"]["model"], "max_tokens": 32000,
                                "system": system,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "\n".join(b.get("text", "") for b in r.json().get("content", [])
                         if b.get("type") == "text").strip()
    raise ValueError(who)


import re as _re
# A model that thinks it has tools writes them out as text: reject that.
ARTIFACTS = _re.compile(r"<tool_call>|```json|File written|\*\*[^*\n]*(Result|Query)\*\*|"
                        r"\"path\": \"|^Let me |\w+ • \w+", _re.M)


def unfence(text: str) -> str:
    """Drop a code fence wrapped around the whole answer (```markdown … ```)."""
    t = text.strip()
    if t.startswith("```") and t.endswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t[: t.rfind("```")]
    return t.strip()


def persona(who: str) -> str:
    """Who the resident is, as its harness tells it (the static prompt)."""
    import logging
    logging.disable(logging.CRITICAL)
    if who == "yue":
        ident = (REPO / "profiles/yue/identity.md").read_text()
        return ident.replace("Kimi K3.1 Pro (Moonshot AI), since October\n25, 2026; before that, K3",
                             "Kimi K3 (Moonshot AI)")
    import importlib
    prompt = importlib.import_module(f"{RESIDENTS[who]['pkg']}.prompt")
    return prompt.SYSTEM_PROMPT


# ---- files ------------------------------------------------------------------------

FILE_TASK = """This is one of your own files: {rel}.
You have no tools in this turn; everything you need is below.

<your_version> is the file as you last wrote it, before today's review.
<reviewed_version> is the same file after a reviewer checked it against your
logs today. The reviewer's facts are verified; the wording is the reviewer's,
not yours.
<review_notes> is the reviewer's changelog.

Write the file again, in your own words. Start from your version and change
it only where the review's facts require. Take the corrected facts (who is
who, names, dates, models, what happened) from the review, but not its
sentences: the reviewed version contains sentences another writer added, and
none of them should appear in your file as written — say each new thing your
own way. Leave out any interpretation you wouldn't stand behind. Keep the
file's purpose and structure. Output only the file's content.

<your_version>
{yours}
</your_version>

<reviewed_version>
{reviewed}
</reviewed_version>

<review_notes>
{notes}
</review_notes>"""

NEW_FILE_TASK = """A reviewer compiled a new file for you today, {rel}, from your own
records (cited in it). The facts are yours; the wording is the reviewer's.
You have no tools in this turn; everything you need is below.

Write the file in your own words, using only facts that are in it — none of
its sentences as written: say each thing your own way. Leave out anything you
wouldn't stand behind. Output only the file's content.

<reviewed_version>
{reviewed}
</reviewed_version>

<review_notes>
{notes}
</review_notes>"""


def earliest(state: Path, rel: str) -> str | None:
    for snap in sorted(state.glob(f".ember_edits/{DATE}*/before")):
        for cand in (snap / rel, snap / "people" / Path(rel).name if rel.startswith("people/") else None):
            if cand and cand.exists():
                return cand.read_text()
    return None


def review_notes(state: Path, rel: str) -> str:
    out = []
    for ch in sorted(state.glob(f".ember_edits/{DATE}*/CHANGES.md")):
        text = ch.read_text()
        name = Path(rel).name
        # the whole changelog is small; keep the sections that mention this file
        parts = [p for p in text.split("\n## ") if name in p or rel in p]
        out.append("\n## ".join(parts) if parts else "")
    return "\n\n".join(x for x in out if x).strip() or "(no notes for this file)"


def do_file(who: str, rel: str, review_dir: Path) -> str:
    state = RESIDENTS[who]["state"]
    live = (state / rel).read_text()
    yours = earliest(state, rel)
    notes = review_notes(state, rel)
    if yours is None:
        prompt = NEW_FILE_TASK.format(rel=rel, reviewed=live, notes=notes)
    else:
        prompt = FILE_TASK.format(rel=rel, yours=yours, reviewed=live, notes=notes)
    for attempt in range(3):
        text = unfence(call(who, persona(who), prompt))
        if not ARTIFACTS.search(text) and len(text) > 0.6 * len(live):
            break
        print(f"  {rel}: rejected attempt {attempt + 1} "
              f"({'tool-use text' if ARTIFACTS.search(text) else 'too short — truncated?'})",
              flush=True)
    else:
        raise RuntimeError("no clean rewrite in 3 attempts")
    out = review_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text.rstrip() + "\n")
    return f"{rel}: {len(live):,} -> {len(text):,} chars"


# ---- session notes ----------------------------------------------------------------

NOTE_TASK = """Here is one of your session notes, written by a different model
from a conversation of yours, and the messages of that conversation from the
room's log. Write the note again yourself, from the messages, following the
instructions above. Output only the note.

<old_note>
{old}
</old_note>

<messages>
{msgs}
</messages>"""


def room_messages(state: Path, stream: str, topic: str, start: str, end: str) -> str:
    from claude_resident.util import safe_filename
    log = state / "channels" / safe_filename(stream) / f"{safe_filename(topic)}.jsonl"
    if not log.exists():
        return "(no log)"
    out = []
    for line in log.read_text().splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if start <= m.get("ts", "") <= end:
            out.append(f"[{m['ts'][:16]}] {m.get('sender', '?')}: {m.get('content', '')}")
    return "\n".join(out)[-60_000:] or "(no messages in that window)"


def note_system(who: str) -> str:
    import importlib
    import logging
    logging.disable(logging.CRITICAL)
    mod = importlib.import_module(f"{RESIDENTS[who]['pkg']}.session_summary")
    return mod.SUMMARY_SYSTEM


def do_notes(who: str, per_topic: int, review_dir: Path, workers: int) -> list[str]:
    state = RESIDENTS[who]["state"]
    jobs = []
    for f in sorted((state / "sessions").rglob("*.jsonl")):
        rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        for i in range(max(0, len(rows) - per_topic), len(rows)):
            jobs.append((f, i, rows[i]))
    system = note_system(who)

    def one(job):
        f, i, row = job
        msgs = room_messages(state, row.get("stream", ""), row.get("topic", ""),
                             row.get("start", ""), row.get("end", "~"))
        return f, i, unfence(call(who, system, NOTE_TASK.format(old=row.get("summary", ""), msgs=msgs)))

    results = {}
    with ThreadPoolExecutor(workers) as ex:
        for f, i, text in ex.map(one, jobs):
            results.setdefault(str(f.relative_to(state)), {})[i] = text
    out = review_dir / "session_notes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    return [f"{len(jobs)} notes rewritten -> {out}"]


def apply_notes(who: str, review_dir: Path):
    state = RESIDENTS[who]["state"]
    results = json.loads((review_dir / "session_notes.json").read_text())
    for rel, by_i in results.items():
        f = state / rel
        rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        for i, text in by_i.items():
            row = rows[int(i)]
            if text and "summary_prev" not in row:
                row["summary_prev"] = row.get("summary", "")
                row["summary"] = text
                row["summary_by"] = "own model, " + datetime.now(timezone.utc).isoformat()
        f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        print(f"applied {len(by_i)} notes in {rel}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("who", choices=list(RESIDENTS))
    ap.add_argument("what", choices=["files", "notes"])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--per-topic", type=int, default=10)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    state = RESIDENTS[a.who]["state"]
    review_dir = state / f".ember_edits/{DATE}-selfwrite/review"
    if a.what == "files":
        if a.apply:
            for rel in a.paths:
                src, dst = review_dir / rel, state / rel
                shutil.copy2(dst, dst.with_name(dst.name + ".before-selfwrite"))
                shutil.copy2(src, dst)
                print(f"applied {rel}")
            return
        def safe(rel):
            try:
                return do_file(a.who, rel, review_dir)
            except Exception as e:
                return f"{rel}: FAILED ({str(e)[:160]})"
        with ThreadPoolExecutor(a.workers) as ex:
            for line in ex.map(safe, a.paths):
                print(line, flush=True)
    else:
        if a.apply:
            apply_notes(a.who, review_dir)
            return
        for line in do_notes(a.who, a.per_topic, review_dir, a.workers):
            print(line, flush=True)


if __name__ == "__main__":
    main()
