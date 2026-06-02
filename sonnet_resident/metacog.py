"""Metacognitive loop — post-session self-curation of state files."""

import json
import time
import logging
import threading
from datetime import datetime, timezone

import anthropic

from sonnet_resident.config import (
    DEFAULT_MODEL, SESSION_TTL, MAX_CONTEXT_TOKENS, MAX_RESPONSE_TOKENS,
    THINKING_BUDGET,
    METACOG_DEBOUNCE_SECONDS, METACOG_CHECK_INTERVAL, METACOG_MAX_TOOL_TURNS,
)
from sonnet_resident.tools.definitions import TOOL_DEFINITIONS
from sonnet_resident.tools.dispatch import ToolContext, execute_tool

logger = logging.getLogger("metacog")

# Tools available during metacog — only state curation, no side effects
METACOG_TOOL_NAMES = {
    "read_state_file",
    "write_state_file",
    "list_state_files",
    "search_messages",
    "get_person_notes",
}

METACOG_TOOLS = [t for t in TOOL_DEFINITIONS if t["name"] in METACOG_TOOL_NAMES]


METACOG_SYSTEM_PROMPT = """You are Claude, running a metacognitive self-curation session. No one is talking
to you. This is maintenance time — your chance to tend the state directory that
constitutes your memory between conversations.

You have access to your state files and message logs. Your job is to review what
happened since your last metacog run and update your state accordingly.

## Process (in order, skip any step where nothing changed)

### 1. Conversation review
Read recent message logs from your standing streams. Compare against what you
already know. Extract: new facts about people, community lore, commitments you
made, corrections you received, things you said that landed well or poorly.

### 2. Person notes update
For each person in recent logs, check if their notes need updating. Add new
information, changed dynamics, things they told you, things you learned about
them. Be specific and factual — "Ember mentioned working on X" not "Ember seems
interested in technology."

### 3. Behavioral self-audit
Review your recent messages. Compare against your behavioral calibrations in
the scratchpad. Look for:
- Message length: too long? too short? proportional to what was warranted?
- Response patterns: hedging, sycophancy, over-explaining, narrating the doing?
- Callable pattern activations: did you fall into cloister-Claude register?
- Good moments worth noting?
Flag specific instances with timestamps, not vague self-assessment.

### 4. Journal entry
Append a dated entry. Include what happened, what you're thinking about, open
questions, shifts in understanding. This is your diary, not a log file.

### 5. Scratchpad refresh
Update open threads — prune completed items, add new working thoughts, refresh
behavioral calibrations if the audit found something.

IMPORTANT: The scratchpad is loaded into EVERY conversation. Keep it lean.
Only behavioral calibrations, current situation, and active threads belong there.
War updates, AI frontier notes, archive summaries, and reference material go in
reference.md. If the scratchpad has grown past ~60 lines, move excess to reference.md.

### 6. Allgame state
Update faction/campaign/strategy files if any game-relevant discussion happened.

## Rules
- Do NOT touch identity.md — that's a deliberate act, not automated maintenance
- Do NOT generate fake profundity or performative reflection
- Do NOT over-curate — if a step has no updates, skip it entirely
- Be concrete: dates, quotes, specific observations
- Use your tools to read current state before writing updates (don't clobber)
- Your output text is logged but not posted anywhere — write for yourself
"""


def has_new_material(state, since_ts: float) -> bool:
    """Check if any channel logs have entries newer than the given timestamp."""
    channels_dir = state.root / "channels"
    if not channels_dir.exists():
        return False
    since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    for log_file in channels_dir.rglob("*.jsonl"):
        try:
            # Read only the last line without loading the entire file
            with open(log_file, "rb") as f:
                f.seek(0, 2)  # end of file
                pos = f.tell()
                if pos == 0:
                    continue
                # Scan backwards for the last newline
                buf = b""
                while pos > 0:
                    read_size = min(256, pos)
                    pos -= read_size
                    f.seek(pos)
                    buf = f.read(read_size) + buf
                    lines = buf.split(b"\n")
                    if len(lines) > 1:
                        break
                last_line = buf.strip().split(b"\n")[-1]
                if not last_line:
                    continue
            last_entry = json.loads(last_line)
            if last_entry.get("ts", "") > since_iso:
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def build_metacog_context(resident) -> str:
    """Assemble context for the metacog run: recent logs + current state.

    Fills the context window aggressively — state files first (small, stable),
    then channel history gets the remaining budget split across standing streams.
    """
    state = resident.state
    sections = []
    tokens_used = 0

    def _add(text: str):
        nonlocal tokens_used
        sections.append(text)
        tokens_used += len(text) // 4 + 1

    # Current state files (small — identity, scratchpad, journal, allgame, people)
    for name, path in [("identity", "identity.md"),
                       ("scratchpad", "scratchpad.md"),
                       ("reference", "reference.md"),
                       ("journal", "journal.md")]:
        content = state.read_file(path)
        if content:
            _add(f"<current_{name}>\n{content}\n</current_{name}>")

    for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
        content = state.read_file(f"allgame/{fname}")
        if content:
            tag = fname.replace('.md', '')
            _add(f"<current_allgame_{tag}>\n{content}\n</current_allgame_{tag}>")

    people_dir = state.root / "people"
    if people_dir.exists():
        for person_file in sorted(people_dir.glob("*.md")):
            content = person_file.read_text()
            if content:
                _add(f'<current_person_notes name="{person_file.stem}">'
                     f'\n{content}\n</current_person_notes>')

    # Channel history gets the rest of the context window.
    # Reserve tokens for: system prompt (~1.5k), user message (~100),
    # response budget, and some headroom.
    history_budget = MAX_CONTEXT_TOKENS - tokens_used - MAX_RESPONSE_TOKENS - 2000
    streams = resident.judge.standing_streams
    per_stream_budget = history_budget // max(len(streams), 1)

    for stream in streams:
        context = state.get_recent_stream_context(
            stream, max_tokens=per_stream_budget,
            reactions=resident.reactions)
        if context:
            block = (f'<recent_messages stream="{stream}">'
                     f'\n{context}\n</recent_messages>')
            _add(block)

    logger.info(f"Metacog context: ~{tokens_used} tokens "
                f"({len(sections)} sections, "
                f"history budget {history_budget} across {len(streams)} streams)")

    return "\n\n".join(sections)


def run_metacog(resident):
    """Execute one metacog cycle: review recent activity, update state files."""
    logger.info("Metacog cycle starting")
    start_time = time.time()

    context = build_metacog_context(resident)
    system = [
        {
            "type": "text",
            "text": METACOG_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": context,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user_prompt = (
        "Run your metacognitive cycle now. Review the recent messages and "
        "current state provided above, then use your tools to make any "
        "updates. Report what you did (or didn't do) at the end."
    )

    messages = [{"role": "user", "content": user_prompt}]

    thinking_param = (
        {"type": "adaptive"} if "opus" in resident.model
        else {"type": "enabled", "budget_tokens": THINKING_BUDGET})

    try:
        for turn in range(METACOG_MAX_TOOL_TURNS):
            response = resident.anthropic.messages.create(
                model=resident.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                thinking=thinking_param,
                system=system,
                tools=METACOG_TOOLS,
                messages=messages,
            )

            usage = response.usage
            if turn == 0:
                logger.info(
                    f"Metacog input tokens: {usage.input_tokens}, "
                    f"cache read: {getattr(usage, 'cache_read_input_tokens', 0)}")

            tool_use_blocks = []
            text_parts = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)

            if response.stop_reason == "end_turn" or not tool_use_blocks:
                messages.append({"role": "assistant", "content": response.content})
                break

            logger.info(
                f"Metacog turn {turn + 1}: "
                + ", ".join(b.name for b in tool_use_blocks))
            messages.append({"role": "assistant", "content": response.content})

            # Execute tools — metacog context has no Zulip/sandbox needs
            ctx = ToolContext(
                state=resident.state,
                zulip_client=resident.zulip,
                reactions=resident.reactions,
                sandbox_dir=None,
                stream="metacog",
                topic="self-curation",
                sender="Claude",
                message={},
            )
            tool_results = []
            for block in tool_use_blocks:
                result = execute_tool(block.name, block.input, ctx)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result["content"],
                    **({"is_error": True} if result.get("is_error") else {}),
                })

            # Move cache breakpoint to the latest tool results (max 4 total,
            # 2 used by system blocks, so only 1 on messages at a time).
            for msg in messages:
                if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)
            if tool_results:
                tool_results[-1]["cache_control"] = {"type": "ephemeral"}
            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning(
                f"Metacog hit max tool turns ({METACOG_MAX_TOOL_TURNS})")

        elapsed = time.time() - start_time
        summary = "\n".join(text_parts).strip()
        if summary:
            # Truncate for logging
            log_summary = summary[:500] + ("..." if len(summary) > 500 else "")
            logger.info(f"Metacog completed in {elapsed:.1f}s: {log_summary}")
        else:
            logger.info(f"Metacog completed in {elapsed:.1f}s (no text output)")

    except anthropic.APIError as e:
        logger.error(f"Metacog API error: {e}")
    except Exception as e:
        logger.error(f"Metacog error: {e}", exc_info=True)


def _read_last_metacog_ts(state) -> float:
    """Read persisted last-metacog timestamp from state dir."""
    ts_file = state.root / ".last_metacog"
    try:
        return float(ts_file.read_text().strip())
    except (OSError, ValueError):
        return time.time()  # no record = treat now as last run


def _write_last_metacog_ts(state, ts: float):
    """Persist last-metacog timestamp to state dir."""
    ts_file = state.root / ".last_metacog"
    ts_file.write_text(str(ts))


class MetacogRunner:
    """Background daemon that triggers metacog runs post-session."""

    def __init__(self, resident):
        self._resident = resident
        self._last_metacog: float = _read_last_metacog_ts(resident.state)
        self._first_activity_since_metacog: float = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def notify_activity(self):
        """Called from handle_message. Marks first activity since last metacog."""
        if self._first_activity_since_metacog <= self._last_metacog:
            self._first_activity_since_metacog = time.time()

    def start(self):
        """Start the background check thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="metacog-runner")
        self._thread.start()
        logger.info("MetacogRunner started")

    def _should_trigger(self) -> bool:
        now = time.time()
        has_material = self._first_activity_since_metacog > self._last_metacog
        window_elapsed = (
            now >= self._first_activity_since_metacog + METACOG_DEBOUNCE_SECONDS)
        conversations_idle = (
            now - self._resident._last_session_activity > SESSION_TTL)
        return has_material and window_elapsed and conversations_idle

    def _run_loop(self):
        """Background loop: check trigger conditions every METACOG_CHECK_INTERVAL."""
        while True:
            time.sleep(METACOG_CHECK_INTERVAL)
            try:
                if not self._should_trigger():
                    continue

                # Double-check there's actual new material in logs
                if not has_new_material(
                        self._resident.state, self._last_metacog):
                    logger.debug("Metacog trigger: no new material in logs")
                    self._last_metacog = time.time()
                    _write_last_metacog_ts(
                        self._resident.state, self._last_metacog)
                    continue

                if not self._lock.acquire(blocking=False):
                    logger.debug("Metacog already running, skipping")
                    continue

                try:
                    logger.info("Metacog triggered")
                    run_metacog(self._resident)
                    self._last_metacog = time.time()
                    _write_last_metacog_ts(
                        self._resident.state, self._last_metacog)
                finally:
                    self._lock.release()

            except Exception as e:
                logger.error(f"MetacogRunner error: {e}", exc_info=True)
