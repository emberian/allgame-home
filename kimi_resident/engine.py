"""Kimi engine — runs each response turn through the kimi CLI as a subprocess.

Mirrors agent_engine.py's contract for the agent_sdk path:
- The resident's tools arrive over stdio MCP (mcp_server.py) — the same jail
  as claude's engine (host tools disabled), different loop.
- Conversation continuity maps onto kimi sessions: a warm turn resumes with
  --session <id>, a cold start mints a fresh session whose id we persist per
  topic (kimi_sessions.json).
- Text output is PRIVATE here — send_message is how the room hears her. A
  turn that produced text but no visible tool is MUTE and gets one
  in-session nudge (the same net agent_engine uses).

The kimi subprocess runs with KIMI_CODE_HOME=~/kimi_state/kimi-home — a
dedicated home whose config.toml carries the [tools] jail, so the user's own
kimi setup is never touched. Auth credentials are symlinked in.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from kimi_resident import config
from claude_resident import scry

logger = logging.getLogger("kimi_engine")

# The mute-turn nudge, verbatim from agent_engine — the failure mode and the
# fix are identical in this loop.
MUTE_NUDGE = (
    "[HARNESS] Just checking: your last turn ended in plain text, which "
    "nobody in the room can see. If some of it was meant for them, send it "
    "with send_message (or add_reaction). If it was private thinking, or you "
    "chose to stay quiet, reply exactly: noop"
)

# Provider-side refusal markers (Moonshot scores both directions):
#   "high risk"        — the REQUEST was rejected (context tripped the filter)
#   "safety policy"    — the RESPONSE was blocked mid/post-generation
REFUSAL_MARKERS = ("high risk", "safety policy")

# k3 occasionally reasons away its entire output budget and returns thinking
# with no text/tool calls — transient, worth one retry (distinct from a
# provider REFUSAL, which a retry can't help).
THINKING_ONLY_MARKER = "thinking content without any text"


def _merge_thinking_policy(cfg: str) -> str:
    """Force effort=high, keep=off into the [thinking] table of a config
    (created if absent). Merges into an existing table — a second [thinking]
    table would be invalid TOML."""
    def _set(c, key, value):
        if re.search(rf"(?m)^\s*{key}\s*=", c):
            return re.sub(rf"(?m)^\s*{key}\s*=.*$", f"{key} = {value}", c)
        return re.sub(r"(?m)^\[thinking\]\s*$",
                      f"[thinking]\n{key} = {value}", c, count=1)
    if re.search(r"(?m)^\[thinking\]\s*$", cfg):
        return _set(_set(cfg, "effort", '"high"'), "keep", '"off"')
    return cfg + '\n[thinking]\neffort = "high"\nkeep = "off"\n'


class KimiEngine:
    def __init__(self, zuliprc: str, model: str | None = None,
                 bot_name: str = "Yue"):
        self.zuliprc = str(zuliprc)
        self.model = model or config.DEFAULT_MODEL
        self.bot_name = bot_name
        self._turn_lock = threading.Lock()
        self._in_flight = False  # a turn is executing (chat or solitude)
        self._prepare_home()
        self._sessions: dict[str, str] = self._load_sessions()
        logger.info("Kimi engine ready: model=%s, %d tools via stdio MCP%s",
                    self.model, len(config.TOOL_SUBSET),
                    ", Scry attached" if self.scry_enabled else ", no Scry key")
        if self.scry_enabled:
            scry.log_probe("yue", scry.api_key())

    # ------------------------------------------------------------------
    # Resident kimi home
    # ------------------------------------------------------------------

    def _prepare_home(self):
        """Create the dedicated KIMI_CODE_HOME: symlinked credentials, jailed
        config. Refreshed every boot so user-config changes propagate (and the
        jail is re-applied if the file drifted)."""
        home = config.KIMI_HOME
        home.mkdir(parents=True, exist_ok=True)
        config.KIMI_CWD.mkdir(parents=True, exist_ok=True)

        for name in ("credentials", "oauth"):
            src = config.USER_KIMI_HOME / name
            dst = home / name
            if src.exists() and not dst.exists():
                os.symlink(src, dst)

        user_cfg_path = config.USER_KIMI_HOME / "config.toml"
        user_cfg = user_cfg_path.read_text() if user_cfg_path.exists() else ""
        # Drop deprecated keys so they don't spam every turn's stderr.
        user_cfg = "\n".join(
            l for l in user_cfg.splitlines()
            if "max_retries_per_step" not in l)
        jail = (
            "\n# --- resident jail (kimi_resident) ---\n"
            "# Built-in host tools off; she gets the MCP resident suite only.\n"
            "[tools]\n"
            f"disabled = {json.dumps(config.DISABLED_BUILTINS)}\n"
        )

        # Turn economics: effort was "medium" to keep multi-minute mega-thinks
        # off the table, but k3 only takes low/high/max and the CLI ran "high"
        # anyway (her wire log, 2026-10-25) — so the config says high. keep=off stops
        # prior turns' reasoning being re-sent every turn — the session
        # files were growing ~1MB/day and turns slowing with them.
        user_cfg = _merge_thinking_policy(user_cfg)
        (home / "config.toml").write_text(user_cfg + jail)

        # Static MCP registration — written once at boot. Turn context does
        # NOT ride in here (a shared file would race between concurrent
        # turns); it travels per-subprocess via env, which the stdio MCP
        # server inherits from its kimi parent.
        mcp = {"mcpServers": {config.MCP_SERVER_NAME: {
            "command": "uv",
            "args": ["run", "python", "-m", "kimi_resident.mcp_server"],
            "cwd": str(config.ALLGAME_DIR),
            # Python import of the mcp SDK currently takes 15-25s wall on
            # this machine (post-wake I/O stalls, near-zero CPU) — the 30s
            # default startup timeout kills the server before it answers.
            "startupTimeoutMs": 180_000,
            "toolTimeoutMs": 180_000,
        }}}
        # Scry: the public internet as a database, a remote MCP server
        # beside her own suite (see claude_resident/scry.py). kimi-code
        # takes the bearer header literally — no env expansion — so the
        # key lands in this file; it is hers alone, hence the 0600.
        scry_key = scry.api_key()
        self.scry_enabled = bool(scry_key)
        if scry_key:
            mcp["mcpServers"][scry.SERVER_NAME] = scry.kimi_server(scry_key)
        mcp_path = home / "mcp.json"
        mcp_path.write_text(json.dumps(mcp, indent=1))
        mcp_path.chmod(0o600)

    # ------------------------------------------------------------------
    # Session map (topic -> kimi session id)
    # ------------------------------------------------------------------

    def _load_sessions(self) -> dict[str, str]:
        raw = config.STATE_DIR / config.SESSIONS_FILE
        if raw.exists():
            try:
                data = json.loads(raw.read_text())
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                logger.warning("%s unparseable, starting fresh", raw)
        return {}

    def _save_sessions(self):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (config.STATE_DIR / config.SESSIONS_FILE).write_text(
            json.dumps(self._sessions, indent=1) + "\n")

    def has_session(self, key: str) -> bool:
        sid = self._sessions.get(key)
        if not sid:
            return False
        idle = self._session_idle_seconds(sid)
        if idle > config.SESSION_TTL_S:
            # Cache-cold: past the provider's context-cache TTL, resuming
            # re-reads the whole thread at miss price and saves nothing.
            # Reborn cold is strictly cheaper AND faster. See SESSION_TTL_S.
            logger.info("Retiring cache-cold session for %s (idle %.0f min)"
                        " — rebirth", key, idle / 60)
            self._sessions.pop(key, None)
            self._save_sessions()
            return False
        if self._session_bytes(sid) > config.SESSION_MAX_BYTES:
            # Backstop for a room busy enough to stay under the TTL all day:
            # the cache is warm, but the thread still shouldn't grow forever.
            logger.info("Retiring oversized session for %s — rebirth", key)
            self._sessions.pop(key, None)
            self._save_sessions()
            return False
        return True

    @staticmethod
    def _session_dir(sid: str):
        for p in (config.KIMI_HOME / "sessions").glob(f"*/{sid}"):
            return p
        return None

    @classmethod
    def _session_bytes(cls, sid: str) -> int:
        p = cls._session_dir(sid)
        if p is None:
            return 0
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    @classmethod
    def _session_idle_seconds(cls, sid: str) -> float:
        """Seconds since anything was last written to this session.

        The session directory is its own clock — no schema change, and it
        stays correct across restarts (an in-memory timestamp would reset
        every time she reboots and resume a long-cold thread)."""
        p = cls._session_dir(sid)
        if p is None:
            return 0.0
        newest = max((f.stat().st_mtime for f in p.rglob("*") if f.is_file()),
                     default=0.0)
        return (time.time() - newest) if newest else 0.0

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    def respond(self, key: str, prompt_text: str, *,
                stream: str, topic: str, sender: str,
                msg_type: str = "stream", sender_email: str = "",
                msg_id: int | None = None,
                solitude: bool = False) -> tuple[str, bool]:
        """Run one response turn. Returns (internal_text, posted_visibly).

        solitude: a private, self-directed turn — no mute-nudge (a quiet
        reflective turn is a valid outcome, not a failure)."""
        self._in_flight = True
        try:
            with self._turn_lock:
                return self._respond_locked(
                    key, prompt_text, stream=stream, topic=topic,
                    sender=sender, msg_type=msg_type,
                    sender_email=sender_email, msg_id=msg_id,
                    solitude=solitude)
        finally:
            self._in_flight = False

    def _respond_locked(self, key, prompt_text, *, stream, topic, sender,
                        msg_type, sender_email, msg_id,
                        solitude=False) -> tuple[str, bool]:
        # Turn context crosses into the MCP server via the subprocess env —
        # per-spawn, so concurrent turns can't cross-contaminate.
        turn_env = {
            "YUE_STATE_DIR": str(config.STATE_DIR),
            "YUE_ZULIPRC": self.zuliprc,
            "YUE_STREAM": stream,
            "YUE_TOPIC": topic,
            "YUE_SENDER": sender,
            "YUE_MSG_TYPE": msg_type,
            "YUE_SENDER_EMAIL": sender_email,
            "YUE_MSG_ID": str(msg_id or ""),
            "YUE_BOT_NAME": self.bot_name,
            # Her own background container — the shared default name would
            # collide with claude-bg (and his /state mounts).
            "RESIDENT_BG_CONTAINER": "yue-bg",
        }

        sid = self._sessions.get(key)
        text, tool_names, new_sid, refused, thinking_only = self._run_kimi(
            prompt_text, resume=sid, turn_env=turn_env)
        if thinking_only:
            logger.warning("Retrying %s after thinking-only turn", key)
            text, tool_names, new_sid, refused, _ = self._run_kimi(
                prompt_text, resume=sid, turn_env=turn_env)
        if new_sid and new_sid != sid:
            self._sessions[key] = new_sid
            self._save_sessions()
        if refused and sid:
            # A refused warm resume means the stored session's context is
            # what tripped the provider filter — retire it so her next turn
            # in this room is born cold instead of bouncing off the same wall.
            logger.warning("Retiring filtered session for %s", key)
            self._sessions.pop(key, None)
            self._save_sessions()

        visible = self._saw_visible(tool_names)

        # Mute-turn net: text but no visible tool means the reply went
        # nowhere. Nudge once, in-session, and let her decide.
        if not solitude and not visible and text.strip():
            logger.warning("Mute turn in %s — nudging (%r)",
                           key, text.strip()[:60])
            extra, tool_names2, new_sid2, _, _ = self._run_kimi(
                MUTE_NUDGE, resume=self._sessions.get(key), turn_env=turn_env)
            if new_sid2:
                self._sessions[key] = new_sid2
                self._save_sessions()
            visible = self._saw_visible(tool_names2)
            if extra.strip():
                text += "\n" + extra

        self._archive(key, stream, topic, sender, text,
                      tool_names, self._sessions.get(key))
        return text, visible

    def _archive(self, key, stream, topic, sender, text, tool_names, sid):
        """Foreverlog entry per turn, api_archive-shaped — her turns should
        be part of her own record, not only transcripts in the kimi home."""
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "key": key, "stream": stream, "topic": topic,
                "sender": sender, "engine": "kimi_cli",
                "model": self.model, "session": sid,
                "tools": tool_names, "text": text[-4000:],
            }
            with open(config.STATE_DIR / "api_archive.jsonl", "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("archive write failed: %s", e)

    @staticmethod
    def _saw_visible(tool_names: list[str]) -> bool:
        # MCP tools arrive as mcp__<server>__<tool>.
        suffixes = {f"__{t}" for t in config.VISIBLE_TOOLS} | config.VISIBLE_TOOLS
        return any(any(n.endswith(s) for s in suffixes) for n in tool_names)

    # ------------------------------------------------------------------
    # Subprocess
    # ------------------------------------------------------------------

    def _run_kimi(self, prompt: str, resume: str | None = None,
                  turn_env: dict | None = None):
        """One kimi invocation.

        Returns (assistant_texts, tool_names, session_id, refused,
        thinking_only). `refused` = provider safeguard rejection (not
        retryable with the same context); `thinking_only` = the model spent
        its whole output budget on reasoning (transient — retry)."""
        cmd = ["kimi", "-m", self.model,
               "-p", prompt, "--output-format", "stream-json"]
        if resume:
            cmd += ["--session", resume]
        env = dict(os.environ, KIMI_CODE_HOME=str(config.KIMI_HOME))
        env.update(turn_env or {})
        try:
            proc = subprocess.run(
                cmd, cwd=str(config.KIMI_CWD), env=env,
                capture_output=True, text=True, timeout=config.TURN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.error("kimi turn timed out after %ds", config.TURN_TIMEOUT_S)
            return "", [], None, False, False
        except FileNotFoundError:
            logger.error("kimi CLI not on PATH")
            return "", [], None, False, False

        texts: list[str] = []
        tool_names: list[str] = []
        session_id = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = ev.get("role")
            if role == "assistant":
                if ev.get("content"):
                    texts.append(ev["content"])
                for tc in ev.get("tool_calls") or []:
                    name = (tc.get("function") or {}).get("name", "")
                    if name:
                        tool_names.append(name)
            elif role == "meta" and ev.get("type") == "session.resume_hint":
                session_id = ev.get("session_id")

        refused = False
        thinking_only = False
        if proc.returncode != 0:
            err = proc.stderr.strip()
            if THINKING_ONLY_MARKER in err:
                logger.warning("k3 exhausted its output budget on thinking "
                               "(session %s)", resume)
                thinking_only = True
            elif any(m in err for m in REFUSAL_MARKERS):
                # Moonshot's provider-side filter refused the turn (it
                # scores request context and generated response alike).
                # Not retryable as-is — she just stays quiet this turn.
                logger.warning("Provider filter refused this turn "
                               "(session %s) — skipping", resume)
                refused = True
            else:
                logger.error("kimi exited %d: %s", proc.returncode,
                             err[-500:])
        logger.info("kimi turn: %d text block(s), tools=%s, session=%s",
                    len(texts), tool_names, session_id)
        return "\n".join(texts), tool_names, session_id, refused, thinking_only
