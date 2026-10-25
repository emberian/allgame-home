"""Tulip's integrations, for residents running on allgame-public.

allgame-public (~/dev/allgame-public, the `resident` package) is the shared
harness: loop, judge, floor, engine, memory, the core tools. What only makes
sense in this community lives here and is loaded by a profile:

    [tools]
    modules = ["tulip_ext"]
    enabled = [..., "tulip"]

Importing this module registers the tools below with the harness's
registry. They are not reimplemented: each one runs the existing
claude_resident implementation through its own dispatcher (execute_tool and
a ToolContext built from the turn), the same way Yue's old CLI harness
served them over MCP — so the path jails, chunking and quirks are the ones
Claude's and Kestrel's tools already have.

start(resident) runs once at startup: the private timer loop (its timers
can watch a Delvetown thread, which the public timers cannot), Scry as an
extra MCP server, and the harness self-view in the state dir.
"""

import logging
import shutil
from pathlib import Path

from claude_resident.tools.definitions import TOOL_DEFINITIONS
from claude_resident.tools.dispatch import ToolContext, execute_tool
from claude_resident.state import StateManager
from resident import profile
from resident.tools import registry

logger = logging.getLogger("tulip_ext")

GROUP = "tulip"

# The tools a Tulip resident has that the public harness doesn't. The public
# harness's own state, messaging, search_messages, fetch_url and game tools
# stay its own (they carry the floor, the draft gate and the shared table).
WRAPPED = [
    # state, beyond the basics
    "glob_state_files", "note_crosstopic", "upload_state_file",
    # reaching Ember, and the room's and her own past
    "send_sysadmin_message", "search_zulip_history", "search_archive",
    # sandbox (Tulip's image and workspace) and background tasks
    "run_sandbox", "get_sandbox_file", "upload_sandbox_file",
    "run_background", "query_background",
    # the web
    "web_search",
    # Delvetown: public reads and the local mirror (no account tools)
    "delvetown_read", "delvetown_search", "delvetown_thread",
    "delvetown_person", "delvetown_new", "delvetown_sql",
    # timers whose watches can follow a Delvetown thread
    "set_timer", "list_timers", "cancel_timer",
]


class TulipState(StateManager):
    """claude_resident's StateManager over the same state dir, for the
    wrapped tools. Two seeds of the base class are not for her: a default
    identity (she has hers) and a copy of Claude's harness. Her harness/
    self-view is the code that actually runs her now."""

    @staticmethod
    def _default_identity() -> str:
        return ""

    def _init_file(self, name, default):
        if default:
            super()._init_file(name, default)

    def _copy_harness_source(self):
        import resident as public
        dest = self.root / "harness"
        if dest.exists():
            for old in dest.rglob("*"):
                if old.is_file():
                    old.unlink()
        dest.mkdir(exist_ok=True)
        sources = [(Path(public.__file__).parent, "resident"),
                   (Path(__file__).parent, "tulip_ext")]
        for src_root, name in sources:
            for f in src_root.rglob("*.py"):
                target = dest / name / f.relative_to(src_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target)


_states: dict[Path, TulipState] = {}
_sandboxes: dict[tuple[str, str], str] = {}


def _state(root: Path) -> TulipState:
    if root not in _states:
        _states[root] = TulipState(root)
    return _states[root]


def _tool_context(ctx) -> ToolContext:
    message = dict(ctx.message or {})
    return ToolContext(
        state=_state(ctx.state.root),
        zulip_client=getattr(ctx.chat, "client", None),
        reactions=None,
        sandbox_dir=_sandboxes.get((ctx.stream, ctx.topic)),
        stream=ctx.stream, topic=ctx.topic, sender=ctx.sender, message=message,
        display_name=profile.current().name)


def _flatten(content) -> str:
    """The harness's tool results are text; image and document blocks from
    the shared implementations are described rather than dropped."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        else:
            parts.append(f"[{b.get('type', 'block')} omitted]")
    return "\n".join(parts) or "(no output)"


def _handler(name: str):
    def run(args, ctx):
        tc = _tool_context(ctx)
        result = execute_tool(name, args, tc)
        if tc.sandbox_dir:
            _sandboxes[(ctx.stream, ctx.topic)] = tc.sandbox_dir
        if tc.restart_requested:
            ctx.restart_requested = True
        out = {"content": _flatten(result.get("content"))}
        if result.get("is_error"):
            out["is_error"] = True
        return out
    return run


def _register():
    defs = {d["name"]: d for d in TOOL_DEFINITIONS}
    missing = [n for n in WRAPPED if n not in defs]
    if missing:
        raise RuntimeError(f"tulip_ext: no claude_resident definition for {missing}")
    for name in WRAPPED:
        d = defs[name]
        registry.register(name, GROUP, d["description"], d["input_schema"], _handler(name))


_register()


def start(resident):
    """Once at startup: the timer loop, Scry, and the harness self-view."""
    from claude_resident import scry
    from claude_resident.timers import start_timer_loop

    _state(resident.state.root)            # builds the harness/ self-view
    start_timer_loop(resident.state.root, resident.run_turn, name="tulip-timers")

    p = profile.current()
    key = scry.api_key()
    if key and "scry" not in p.mcp:
        p.mcp[scry.SERVER_NAME] = scry.sdk_server(key)
        p.disallowed_tools += scry.sdk_disallowed(scry.log_probe(p.name, key))
        p.identity += "\n" + scry.PROMPT_GUIDANCE
        logger.info("Scry attached")
