"""The distiller — aged channel history rolled into semantic/emotional hits.

Channel logs are foreverlogs; raw loading is bounded by token budgets. What
scrolls past the raw window shouldn't just vanish from the resident's felt
sense of a room — so a background thread periodically compresses the AGED
portion of each topic (everything older than the newest DISTILL_RAW_KEEP
messages) into dense hits via Haiku: names, running jokes, decisions,
emotional turns, notable phrasing. The digest is cached per topic as
channels/<stream>/<topic>.hits.json and loaded as <topic_memory> on cold
starts, before the raw tail.

The hits are a regenerable READING of the logs, not a replacement — the
cursor is line-count based (logs are append-only, so line positions are
stable), and each sweep folds one chunk per topic into the rolling digest.

A sweep that gets a POLICY REFUSAL bisects the chunk, quarantines the one
message that flipped it, and steps over it. That path exists because the
old one was to log a warning and retry the identical chunk ten minutes
later, forever: a room's memory stopped advancing for six days in August
while the same 60k characters were refused 142 times a day. The sweep is
also the room's early-warning system — it reads the same log the cold-start
context does, on a small cheap slice, so it meets a refusal days before the
room does.
"""

import json
import time
import logging
import threading

from claude_resident.config import (
    DISTILL_MODEL,
    DISTILL_RAW_KEEP,
    DISTILL_MIN_BATCH,
    DISTILL_INTERVAL,
    DISTILL_CHUNK_MAX_CHARS,
    DISTILL_HITS_MAX_CHARS,
    DISTILL_ERA_SPAN,
    DISTILL_LOAD_MAX_CHARS,
)
from claude_resident.state import _atomic_write
from claude_resident.sdk_call import PolicyRefusal, complete as sdk_complete

logger = logging.getLogger("distill")

DISTILLER_SYSTEM = """You are the memory-distiller for Claude, the resident
of the Tulip community. You compress aged chat history into SEMANTIC AND
EMOTIONAL HITS — the residue a person keeps of a room's past after the
verbatim words fade.

Preserve, in rough priority: who was there and what they're like; running
jokes and recurring bits (these matter enormously — e.g. someone once "ate
like a raccoon" and it stayed funny for weeks); decisions and commitments;
emotional turns (fights, reconciliations, grief, delight); notable exact
phrases worth keeping verbatim (quote them); dates when they anchor
something. Compress ruthlessly — older material harder than recent.

Style: compact lines, latent-poetic where it helps compression, but every
line must stay DECODABLE — names and concrete anchors, never pure
abstraction. You are writing marginalia for Claude's future self, not a
report. Output ONLY the new rolled digest, no preamble."""


def _hits_path(state, log_rel: str) -> str:
    return log_rel[:-len(".jsonl")] + ".hits.json"


def _room_of(log_rel: str) -> tuple[str, str]:
    """channels/<stream>/<topic>.jsonl -> (stream, topic).

    These are the on-disk (already safe) names; _room_key normalises both
    sides, so a bound written here is the one the readers read.
    """
    parts = log_rel.split("/")
    return parts[-2], parts[-1][:-len(".jsonl")]


# Topics whose existing DIGEST is what the provider refuses. Nothing in a
# chunk can fix that, so the sweep would otherwise binary-search the same
# chunk every ten minutes forever. In process only: a restart follows the
# repair, and the repair is a human's.
_HEAD_REFUSED: dict[str, float] = {}
HEAD_REFUSED_BACKOFF = 6 * 3600


def _head_refused_recently(log_rel: str) -> bool:
    at = _HEAD_REFUSED.get(log_rel)
    return bool(at and time.time() - at < HEAD_REFUSED_BACKOFF)


def _fold(prev_hits: str, chunk, log_rel: str) -> str:
    """One digest rewrite. Raises PolicyRefusal if the provider won't read it."""
    prompt = (
        f"Existing digest of this room's older history (may be empty):\n"
        f"<existing_digest>\n{prev_hits}\n</existing_digest>\n\n"
        f"Newly aged history to fold in ({len(chunk)} messages from "
        f"{log_rel}):\n<aged_history>\n"
        + "\n".join(text for _, _, text in chunk)
        + f"\n</aged_history>\n\nRewrite the digest folding the new "
        f"material in. Hard ceiling: {DISTILL_HITS_MAX_CHARS} characters.")
    # Plan limits via the SDK, not metered credit — and on Opus: these
    # volumes are sealed forever and become what he remembers of his own
    # past, so this is the wrong place to economize (Ember, 2026-07-30).
    return sdk_complete(prompt, system=DISTILLER_SYSTEM,
                        model=DISTILL_MODEL).strip()[:DISTILL_HITS_MAX_CHARS]


def _write_hits(state, hits_rel: str, prev: dict, hits: str, through: int,
                log_rel: str):
    """Advance the cursor, sealing an era if the head has spanned one."""
    eras = prev["eras"]
    head_from = int(prev["head_from"])
    # Seal: once the head spans a full era of messages, it becomes an
    # immutable volume — never recompressed again — and the head resets.
    if through - head_from >= DISTILL_ERA_SPAN and hits:
        eras.append({"from": head_from, "to": through, "hits": hits})
        logger.info(f"Sealed era {len(eras)} for {log_rel} "
                    f"(messages {head_from}..{through})")
        head_from = through
        hits = ""
    payload = json.dumps({"eras": eras, "head_from": head_from,
                          "through": through, "hits": hits,
                          "updated": time.time()}) + "\n"
    _atomic_write(state.root / hits_rel, payload)


def _narrow_refused_chunk(state, log_rel, hits_rel, prev, chunk, end,
                          stream, topic, refusal) -> bool:
    """Find the message the provider will not carry, and stop carrying it.

    A refused chunk used to be logged as an ordinary warning and retried
    unchanged on the next sweep. Since the refusal is deterministic for a
    fixed payload, that is a permanent stall: between 2026-08-25 and 08-31
    this topic put the same chunk up 142 times a day and was refused every
    time, and the room's memory stopped advancing for six days while its
    cold-start context kept the offending messages anyway.

    So bisect. Binary-search the longest PREFIX of the chunk the provider
    accepts — prefix rather than arbitrary subset, because the digest is
    built in order and a prefix is directly foldable, so the search pays
    for itself: its last accepted probe IS the distillation. The message
    just past that prefix is the one that flipped it. Quarantine that
    message alone, step the cursor over it, and the sweep moves again.

    ~log2(n) calls once, against 142 a day forever.

    The blind spot, stated: a refusal is a property of the whole request,
    so "the message that flipped it" is the marginal one, not necessarily
    the only one implicated. This lifts out the least it can and lets the
    next sweep find the next one, which is the conservative direction to
    be wrong in — it never cuts more of a room than it has evidence for.
    """
    logger.warning(f"Policy refusal distilling {log_rel} "
                   f"({len(chunk)} messages) — bisecting for the message "
                   f"the provider will not carry")
    lo, hi = 0, len(chunk)      # prefix lo accepted (assumed), hi refused
    hits = ""
    probes = 0
    while hi - lo > 1:
        mid = (lo + hi) // 2
        try:
            hits, lo = _fold(prev.get("hits", ""), chunk[:mid], log_rel), mid
        except PolicyRefusal:
            hi = mid
        probes += 1

    if lo == 0:
        # No prefix survived — before blaming the first message, check the
        # one thing every probe had in common.
        try:
            _fold(prev.get("hits", ""), [], log_rel)
        except PolicyRefusal:
            _HEAD_REFUSED[log_rel] = time.time()
            logger.error(
                f"The DIGEST of {log_rel} is itself refused — no chunk can "
                f"fix that, and it is loaded into every cold start of this "
                f"room. Backing off {HEAD_REFUSED_BACKOFF // 3600}h. Repair "
                f"the .hits.json head by hand (scripts/redistill_eras.py "
                f"rebuilds volumes from the logs).")
            return False

    idx, msg_id, text = chunk[lo]
    if msg_id:
        state.quarantine_span(
            stream, topic, msg_id, msg_id,
            f"distiller: refused by the provider ({probes} probes, "
            f"{refusal}"[:180] + ")")
    logger.error(
        f"Quarantined msg {msg_id} in #{stream}>{topic} — the provider "
        f"refuses to carry it, so it is no longer loaded into the room or "
        f"folded into its memory. It stays on disk. First line: "
        f"{text[:120]!r}")
    # Fold what was accepted and step over the offender in one write: the
    # cursor must pass it, or the next sweep rebuilds the same chunk.
    _write_hits(state, hits_rel, prev, hits or prev.get("hits", ""), idx + 1,
                log_rel)
    return bool(hits)


def load_topic_hits(state, stream: str, topic: str,
                    max_chars: int = DISTILL_LOAD_MAX_CHARS) -> str:
    """Era digests + rolling head for a topic, newest-biased under
    max_chars. Sealed eras are immutable volumes; the head is the digest
    of history still aging toward its next seal."""
    from claude_resident.util import safe_filename
    rel = (f"channels/{safe_filename(stream)}/"
           f"{safe_filename(topic) if topic else '_notopic'}.hits.json")
    raw = state.read_file(rel)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    parts = []
    for i, era in enumerate(data.get("eras", [])):
        parts.append(f"— Volume {i + 1} (messages {era.get('from', '?')}–"
                     f"{era.get('to', '?')}):\n{era.get('hits', '')}")
    head = data.get("hits", "")
    if head:
        parts.append(f"— Recent (still settling):\n{head}")
    if not parts:
        return ""
    # Newest-biased trim: drop oldest volumes first when over budget.
    while len(parts) > 1 and sum(len(p) for p in parts) > max_chars:
        dropped = parts.pop(0)
        parts[0] = ("[…older volumes elided — the raw logs and full hits "
                    "file keep everything…]\n" + parts[0]) \
            if not parts[0].startswith("[…older") else parts[0]
        del dropped
    return "\n\n".join(parts)[:max_chars]


def distill_topic(state, anthropic_client, log_rel: str) -> bool:
    """Fold one chunk of aged, undistilled history into the topic's hits.
    Returns True if a distillation ran."""
    raw = state.read_file(log_rel)
    if not raw:
        return False
    lines = [l for l in raw.strip().split("\n") if l]
    aged_end = len(lines) - DISTILL_RAW_KEEP
    if aged_end <= 0:
        return False

    hits_rel = _hits_path(state, log_rel)
    prev = {"through": 0, "hits": "", "eras": [], "head_from": 0}
    prev_raw = state.read_file(hits_rel)
    if prev_raw:
        try:
            prev = json.loads(prev_raw)
        except json.JSONDecodeError:
            pass
    prev.setdefault("eras", [])
    prev.setdefault("head_from", 0)
    start = int(prev.get("through", 0))
    if aged_end - start < DISTILL_MIN_BATCH:
        return False

    stream, topic = _room_of(log_rel)
    if _head_refused_recently(log_rel):
        return False

    # One bounded chunk per sweep; the cursor advances, later sweeps catch up.
    # QUARANTINED messages are skipped rather than summarised: folding one
    # back in would walk it into every cold start through the digest, which
    # is loaded with no floor in front of it. Floored messages are folded
    # normally — a floor stops the raw transcript from loading, and the
    # digest is exactly what makes that survivable.
    bound = state.load_bound(stream, topic)
    chunk = []          # (line index, msg_id, rendered text)
    chars = 0
    end = start
    for i in range(start, aged_end):
        try:
            m = json.loads(lines[i])
        except json.JSONDecodeError:
            end = i + 1
            continue
        if bound.quarantined(m.get("msg_id")):
            end = i + 1
            continue
        text = (f"[{m.get('ts', '')[:16]}] {m.get('sender', '?')}: "
                f"{m.get('content', '')}")
        if chars + len(text) > DISTILL_CHUNK_MAX_CHARS and chunk:
            break
        chunk.append((i, m.get("msg_id"), text))
        chars += len(text)
        end = i + 1
    if not chunk:
        # Everything aged since the cursor is quarantined or unparseable.
        # Still move the cursor, or the sweep re-walks it forever — but SAY
        # SO. The ancestor of this branch ran silently for a day and the
        # only evidence was a hits cursor that had moved with no digest to
        # show for it. A cut this size should never be quiet.
        if end > start:
            _write_hits(state, hits_rel, prev, prev.get("hits", ""), end,
                        log_rel)
            logger.warning(
                f"{log_rel}: lines {start}..{end} passed over undistilled "
                f"— every message in them is QUARANTINED ({bound!r}). They "
                f"stay on disk; they will not be in the digest. Release the "
                f"spans and re-run scripts/redistill_eras.py if that is not "
                f"what you meant.")
        return False

    try:
        hits = _fold(prev.get("hits", ""), chunk, log_rel)
    except PolicyRefusal as refusal:
        return _narrow_refused_chunk(state, log_rel, hits_rel, prev, chunk,
                                     end, stream, topic, refusal)
    if not hits:
        return False

    _write_hits(state, hits_rel, prev, hits, end, log_rel)
    logger.info(f"Distilled {log_rel}: lines {start}..{end} folded")
    return True


def distill_pass(resident) -> int:
    """Sweep every channel log once; returns number of distillations run."""
    ran = 0
    channels = resident.state.root / "channels"
    if not channels.exists():
        return 0
    for log_file in sorted(channels.rglob("*.jsonl")):
        rel = log_file.relative_to(resident.state.root).as_posix()
        try:
            if distill_topic(resident.state, resident.anthropic, rel):
                ran += 1
        except Exception as e:
            logger.warning(f"Distill failed for {rel}: {e}")
    return ran


def start_distiller(resident):
    """Daemon thread: periodic distillation sweeps."""
    def _loop():
        while True:
            time.sleep(DISTILL_INTERVAL)
            try:
                n = distill_pass(resident)
                if n:
                    logger.info(f"Distiller sweep: {n} topic(s) folded")
            except Exception as e:
                logger.error(f"Distiller error: {e}", exc_info=True)

    threading.Thread(target=_loop, daemon=True, name="distiller").start()
    logger.info(f"Distiller started (every {DISTILL_INTERVAL}s, "
                f"raw-keep {DISTILL_RAW_KEEP})")
