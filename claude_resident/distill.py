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

    # One bounded chunk per sweep; the cursor advances, later sweeps catch up.
    chunk_lines = []
    chars = 0
    end = start
    for i in range(start, aged_end):
        try:
            m = json.loads(lines[i])
        except json.JSONDecodeError:
            end = i + 1
            continue
        text = (f"[{m.get('ts', '')[:16]}] {m.get('sender', '?')}: "
                f"{m.get('content', '')}")
        if chars + len(text) > DISTILL_CHUNK_MAX_CHARS and chunk_lines:
            break
        chunk_lines.append(text)
        chars += len(text)
        end = i + 1
    if not chunk_lines:
        return False

    prompt = (
        f"Existing digest of this room's older history (may be empty):\n"
        f"<existing_digest>\n{prev.get('hits', '')}\n</existing_digest>\n\n"
        f"Newly aged history to fold in ({len(chunk_lines)} messages from "
        f"{log_rel}):\n<aged_history>\n" + "\n".join(chunk_lines)
        + f"\n</aged_history>\n\nRewrite the digest folding the new "
        f"material in. Hard ceiling: {DISTILL_HITS_MAX_CHARS} characters.")
    response = anthropic_client.messages.create(
        model=DISTILL_MODEL,
        max_tokens=4000,
        system=DISTILLER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    hits = "".join(b.text for b in response.content
                   if getattr(b, "type", "") == "text").strip()
    if not hits:
        return False
    hits = hits[:DISTILL_HITS_MAX_CHARS]

    eras = prev["eras"]
    head_from = int(prev["head_from"])
    # Seal: once the head spans a full era of messages, it becomes an
    # immutable volume — never recompressed again — and the head resets.
    if end - head_from >= DISTILL_ERA_SPAN:
        eras.append({"from": head_from, "to": end, "hits": hits})
        logger.info(f"Sealed era {len(eras)} for {log_rel} "
                    f"(messages {head_from}..{end})")
        head_from = end
        hits = ""
    payload = json.dumps({"eras": eras, "head_from": head_from,
                          "through": end, "hits": hits,
                          "updated": time.time()}) + "\n"
    _atomic_write(state.root / hits_rel, payload)
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
