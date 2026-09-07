#!/usr/bin/env python3
"""Ask the provider, before the room does, whether a context will be refused.

A policy refusal on a persistent room's cold start is not recoverable in
flight: every account sees the same context, so rotation cannot fix it, and
there is no bound session to retire because the poison is in the birth
payload itself. The engine's only remaining move is to load LESS — which
means someone has to decide where the line goes.

That decision has been made blind. The August incident cost three days of a
mute tavern to redaction attempts that each looked plausible and each lost
(see 8e2552d), because nobody could ask the question directly: *would this
payload be accepted?* This asks it. One probe renders the exact birth
payload the engine would build at a candidate floor and sends it through the
same SDK door the resident's own turns use, with nothing to do but say a
word back. Accepted or refused, measured rather than guessed.

    python scripts/refusal_probe.py --floor 0            # reproduce the failure
    python scripts/refusal_probe.py --floor 58131        # test one candidate
    python scripts/refusal_probe.py --search             # lowest floor that passes
    python scripts/refusal_probe.py --floor 0 --dry-run  # render only, no call

A probe costs plan quota, not metered credit (sdk_call's door), and a
refused probe is a deliberate AUP trip on whichever account auth hands it.
That is the same trip the distiller has been making every ten minutes for
weeks, but it is not free — keep --search shallow and prefer named
candidates when you already have a hypothesis.

--search returns the LOWEST passing floor, i.e. the one that forgets least.
It assumes acceptance is monotone in the floor (raising it can only remove
context), which is true of the payload but not guaranteed of the classifier;
the result is a candidate to confirm, not a proof.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_resident.agent_engine import AgentEngine
from claude_resident.config import AGENT_SDK_MODEL, DEFAULT_STATE_DIR
from claude_resident.util import is_policy_refusal
from claude_resident.state import StateManager


class _Shim:
    """Just enough resident for _birth_payload to be the real one.

    The payload must come from the engine's own assembly, not a
    reconstruction of it — a probe against a mirror of the loader measures
    the mirror. Reactions are empty here where the live process has
    whatever it has accumulated; that moves a few characters of suffix,
    not a classifier.
    """

    def __init__(self, state):
        self.resident = type("R", (), {"state": state, "reactions": {}})()


def render(state, stream: str, topic: str, floor: int = 0, spans=()) -> str:
    """The payload the engine would build under a candidate bound.

    Overriding the two accessors rather than the file means a candidate can
    be measured without writing it down first — a quarantine you have not
    committed to yet is exactly the thing you want to test.
    """
    state.context_floor = lambda s, t, _f=floor: _f
    state.context_quarantine = lambda s, t, _sp=list(spans): _sp
    return AgentEngine._birth_payload(_Shim(state), stream, topic)


def parse_span(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    return (int(lo), int(hi or lo))


def probe(payload: str, model: str) -> tuple[bool, str]:
    """True if the provider accepted the context. Returns (accepted, note)."""
    from claude_resident.sdk_call import complete
    prompt = (payload + "\n\n[This is a delivery check, not a turn. Reply "
              "with the single word: ok]")
    try:
        return True, complete(prompt, system="", model=model,
                              max_turns=1).strip()[:80]
    except Exception as e:
        if is_policy_refusal(e):
            return False, "policy refusal"
        # The SDK degrades a refusal it saw on the wire into "error result:
        # success" with the reason stripped — agent_engine keeps it off the
        # exception too. An opaque failure here is far more likely to be a
        # refusal than a coincidence, so say so rather than call it a pass.
        return False, f"opaque failure (probably refusal): {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", default="clankerville")
    ap.add_argument("--topic", default="tavern")
    ap.add_argument("--floor", type=int, action="append", default=[],
                    help="candidate floor; repeatable, probed in order")
    ap.add_argument("--quarantine", type=parse_span, action="append",
                    default=[], metavar="LO-HI",
                    help="candidate quarantine span, inclusive; repeatable. "
                         "Applied to every floor probed.")
    ap.add_argument("--search", action="store_true",
                    help="binary-search the lowest floor that is accepted")
    ap.add_argument("--dry-run", action="store_true",
                    help="render and size the payload, make no call")
    # The engine's model, [1m] suffix and all: a birth payload is
    # ~450k tokens, so probing on the 200k default would fail for
    # window overflow and read as a refusal.
    ap.add_argument("--model", default=AGENT_SDK_MODEL)
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = ap.parse_args()

    state = StateManager(args.state_dir)
    live = state.context_floor(args.stream, args.topic)
    live_q = state.context_quarantine(args.stream, args.topic)
    print(f"#{args.stream}>{args.topic}  live floor: {live}  "
          f"live quarantine: {live_q or 'none'}")
    if args.quarantine:
        print(f"probing with candidate quarantine: {args.quarantine}")

    def run(floor):
        payload = render(state, args.stream, args.topic, floor,
                         args.quarantine)
        head = f"floor={floor:<8} {len(payload):>9,} chars "
        if args.dry_run:
            print(head + "(dry run)")
            return None
        ok, note = probe(payload, args.model)
        print(head + ("ACCEPTED" if ok else "REFUSED") + f"  {note}")
        return ok

    if args.search:
        ids = sorted({m for m in _msg_ids(state, args.stream, args.topic)})
        if not ids:
            sys.exit("no messages")
        lo, hi = 0, len(ids) - 1          # lo may refuse; hi is bare present
        if run(ids[hi]) is False:
            sys.exit("even the bare room is refused — the trigger is not in "
                     "the loaded history")
        if run(0) is True:
            sys.exit("floor 0 is accepted — nothing to search for")
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if run(ids[mid]):
                hi = mid
            else:
                lo = mid
        print(f"\nlowest passing floor: {ids[hi]}  "
              f"(forgets through msg_id {ids[hi]})")
        return

    for floor in (args.floor or [live]):
        run(floor)


def _msg_ids(state, stream, topic):
    import json
    from claude_resident.util import safe_filename
    path = (state.root / f"channels/{safe_filename(stream)}/"
            f"{safe_filename(topic)}.jsonl")
    for line in path.read_text().strip().split("\n"):
        if line:
            try:
                mid = json.loads(line).get("msg_id")
            except json.JSONDecodeError:
                continue
            if mid:
                yield mid


if __name__ == "__main__":
    main()
