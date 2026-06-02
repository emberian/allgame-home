"""Self-modification (edit_harness) and git integration."""

import ast
import sys
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from sonnet_resident.config import HARNESS_DIR

logger = logging.getLogger("selfmod")


def edit_harness(old_string: str, new_string: str, commit_message: str,
                 state, file: str | None = None) -> dict:
    """Edit the harness source code with git safety.

    If file is specified, edit that file within the package.
    Otherwise, auto-detect by searching all package .py files.
    """
    if not old_string:
        return {"content": "old_string is required.", "is_error": True}
    if old_string == new_string:
        return {"content": "old_string and new_string are identical.",
                "is_error": True}

    pkg_dir = Path(__file__).resolve().parent

    # Determine target file
    if file:
        target = pkg_dir / file
        if not target.exists():
            return {"content": f"File not found: {file}", "is_error": True}
    else:
        # Auto-detect: search all .py files in the package
        candidates = []
        for py_file in pkg_dir.rglob("*.py"):
            source = py_file.read_text()
            count = source.count(old_string)
            if count > 0:
                candidates.append((py_file, count))

        if not candidates:
            return {"content": "old_string not found in any harness source file.",
                    "is_error": True}
        if len(candidates) > 1:
            files_list = ", ".join(
                str(f.relative_to(pkg_dir)) for f, _ in candidates)
            return {"content": f"old_string found in multiple files: {files_list}. "
                    "Specify file= to disambiguate.", "is_error": True}
        target, count = candidates[0]
        if count > 1:
            return {"content": f"old_string found {count} times in "
                    f"{target.relative_to(pkg_dir)} — must be unique. "
                    "Provide more context.", "is_error": True}

    try:
        source = target.read_text()

        count = source.count(old_string)
        if count == 0:
            return {"content": "old_string not found in target file.",
                    "is_error": True}
        if count > 1:
            return {"content": f"old_string found {count} times — must be unique.",
                    "is_error": True}

        # Git: commit current state before editing
        subprocess.run(
            ["git", "add", "-A"],
            cwd=HARNESS_DIR, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m",
             f"Pre-edit checkpoint (before: {commit_message})"],
            cwd=HARNESS_DIR, capture_output=True, timeout=10)

        # Apply the edit
        new_source = source.replace(old_string, new_string, 1)
        target.write_text(new_source)

        # Verify parse
        try:
            ast.parse(new_source, filename=str(target))
        except SyntaxError as parse_err:
            target.write_text(source)
            error_msg = f"{parse_err.msg} (line {parse_err.lineno})"
            logger.warning(
                f"Harness edit rolled back — parse failed: {error_msg}")
            return {"content": f"Edit rolled back — parse error:\n{error_msg}",
                    "is_error": True}

        # Git: commit the edit
        subprocess.run(
            ["git", "add", str(target)],
            cwd=HARNESS_DIR, capture_output=True, timeout=10)
        result = subprocess.run(
            ["git", "commit", "-m", f"Claude self-edit: {commit_message}"],
            cwd=HARNESS_DIR, capture_output=True, text=True, timeout=10)

        rel_path = target.relative_to(pkg_dir)
        logger.info(f"Harness edited ({rel_path}) and committed: {commit_message}")

        # Refresh the state directory copy
        try:
            state._copy_harness_source()
        except Exception:
            pass

        return {"content": (
            f"Edit applied to {rel_path} and committed.\n"
            f"Git: {result.stdout.strip()}\n"
            f"The event loop will auto-restart after this response completes "
            f"to load your changes."
        )}

    except Exception as e:
        logger.error(f"Harness edit error: {e}", exc_info=True)
        return {"content": f"Edit failed: {e}", "is_error": True}


def last_commit_is_self_edit() -> bool:
    """Check if the most recent git commit was a Claude self-edit."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=HARNESS_DIR, capture_output=True, text=True, timeout=5)
        return result.stdout.strip().startswith("Claude self-edit:")
    except Exception:
        return False


def rollback_last_commit():
    """Revert the most recent git commit (assumed to be a bad self-edit)."""
    try:
        result = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=HARNESS_DIR, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.warning(f"Rolled back self-edit: {result.stdout.strip()}")
        else:
            logger.error(f"Rollback failed: {result.stderr}")
    except Exception as e:
        logger.error(f"Rollback error: {e}")


def notify_claude(state_dir: Path, msg: str):
    """Best-effort write to Claude's scratchpad so it sees what happened."""
    try:
        from sonnet_resident.state import StateManager
        state = StateManager(state_dir)
        state.append_file("scratchpad.md",
            f"\n\n---\n[SUPERVISOR {datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass
