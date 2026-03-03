"""Background container and mirror council tools."""

import json
import logging
import subprocess
from datetime import datetime, timezone

from claude_resident.config import BG_CONTAINER_NAME
from claude_resident.sandbox import ensure_bg_container, run_background

logger = logging.getLogger("tools.background")


def run_background_tool(inp: dict, state_root) -> dict:
    command = inp.get("command", "")
    files = inp.get("files", {})
    timeout = min(inp.get("timeout", 120), 300)
    return run_background(command, files, state_root, timeout)


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
        check = subprocess.run(
            ["docker", "exec", BG_CONTAINER_NAME, "bash", "-c", cmd],
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
