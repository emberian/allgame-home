"""Background container and mirror council tools."""

import json
import logging
import os
import shlex
import subprocess
from datetime import datetime, timezone

from glm_resident.config import BG_CONTAINER_NAME
from glm_resident.sandbox import (
    ensure_bg_container, run_background, _combine_stdio)

logger = logging.getLogger("tools.background")


def run_background_tool(inp: dict, state_root) -> dict:
    command = inp.get("command", "")
    files = inp.get("files", {})
    timeout = min(inp.get("timeout", 120), 300)
    return run_background(command, files, state_root, timeout)


def query_background_tool(inp: dict, state_root) -> dict:
    """Query the background container: ps, read_file, ls, tail_log."""
    action = inp.get("action", "ps")
    path = inp.get("path", "/workspace")
    try:
        lines = min(max(int(inp.get("lines", 50)), 1), 500)
    except (TypeError, ValueError):
        lines = 50

    if not ensure_bg_container(state_root):
        return {"content": "Background container not running.", "is_error": True}

    qpath = shlex.quote(path)
    cmd_map = {
        "ps": "ps aux --sort=-%mem",
        "read_file": f"cat {qpath}",
        "ls": f"ls -lah {qpath}",
        "tail_log": f"tail -n {lines} {qpath}",
    }
    cmd = cmd_map.get(action)
    if not cmd:
        return {"content": f"Unknown action: {action}", "is_error": True}

    try:
        result = subprocess.run(
            ["docker", "exec", BG_CONTAINER_NAME, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=15)
        output = _combine_stdio(result)
        if len(output) > 30_000:
            output = output[:30_000] + "\n\n[truncated at 30k chars]"
        return {"content": output}
    except subprocess.TimeoutExpired:
        return {"content": "Query timed out (15s)", "is_error": True}
    except Exception as e:
        return {"content": f"Query error: {e}", "is_error": True}


def run_mirror_council_tool(inp: dict, state) -> dict:
    """Tool handler: run the mirror council on a draft."""
    draft = inp.get("draft", "")
    context = inp.get("context", "")
    max_rounds = min(inp.get("max_rounds", 2), 4)

    if not draft:
        return {"content": "Error: draft is required", "is_error": True}

    if not ensure_bg_container(state.root):
        return {"content": "Error: background container unavailable",
                "is_error": True}

    final_draft, council_log = _run_mirror_council(
        state, context, draft, max_rounds=max_rounds)

    if council_log:
        state.append_file("council_log.md",
            f"\n---\n[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}] "
            f"(tool invocation)\n{council_log}\n")

    return {"content": (
        f"Council deliberation complete.\n\n"
        f"=== COUNCIL LOG ===\n{council_log}\n\n"
        f"=== FINAL DRAFT ===\n{final_draft}"
    )}


def _run_mirror_council(state, context: str, draft: str,
                        max_rounds: int = 4,
                        council_size: int = 3) -> tuple[str, str]:
    """Run the mirror council on a draft response."""
    input_path = state.root / "bg_workspace" / ".council_input.json"
    try:
        council_input = {
            "context": context, "draft": draft,
            "max_rounds": max_rounds, "council_size": council_size
        }
        input_path.write_text(json.dumps(council_input))

        cmd = ("cd /workspace && python3 -c \""
               "import json, council; "
               "inp = json.load(open('.council_input.json')); "
               "r = council.run_council(inp['context'], inp['draft'], "
               "max_rounds=inp['max_rounds'], council_size=inp['council_size']); "
               "print(council.format_council_log(r)); "
               "print('===FINAL==='); "
               "print(r['final_draft'])\"")
        # The council is the one internal consumer of the API key; hand it in
        # per-exec rather than baking it into the container env (see
        # ensure_bg_container). Model-authored run_background/query commands
        # therefore never see these credentials.
        check = subprocess.run(
            ["docker", "exec",
             "-e", f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
             "-e", f"KAGI_API_KEY={os.environ.get('KAGI_API_KEY', '')}",
             BG_CONTAINER_NAME, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=180)
        output = check.stdout
        if "===FINAL===" in output:
            parts = output.split("===FINAL===", 1)
            council_log = parts[0].strip()
            final_draft = parts[1].strip()
            logger.info(
                f"Mirror council completed: {len(council_log)} chars log")
            return final_draft, council_log
        else:
            logger.warning(
                f"Mirror council unexpected output: {output[:500]}")
            if check.stderr:
                logger.warning(
                    f"Mirror council stderr: {check.stderr[:500]}")
            return draft, f"Council inconclusive:\n{output[:1000]}"
    except subprocess.TimeoutExpired:
        logger.warning("Mirror council timed out")
        return draft, "Council timed out (180s)"
    except Exception as e:
        logger.warning(f"Mirror council error: {e}")
        return draft, f"Council error: {e}"
    finally:
        input_path.unlink(missing_ok=True)
