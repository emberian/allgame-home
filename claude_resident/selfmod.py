"""Self-modification (edit_harness) and git integration."""

import ast
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from claude_resident.config import HARNESS_DIR

logger = logging.getLogger("selfmod")


def _git_run(args: list[str], **kw):
    return subprocess.run(
        ["git", *args],
        cwd=HARNESS_DIR, capture_output=True, text=True, timeout=10, **kw)


def _verify_parse(path: Path, source: str) -> str | None:
    """Return error message string if parse fails, else None.
    Non-.py files are accepted as-is (no parse step)."""
    if path.suffix != ".py":
        return None
    try:
        ast.parse(source, filename=str(path))
        return None
    except SyntaxError as e:
        return f"{e.msg} (line {e.lineno})"


def edit_harness(old_string: str, new_string: str, commit_message: str,
                 state, file: str | None = None,
                 edits: list[dict] | None = None,
                 action: str = "edit") -> dict:
    """Modify the live harness source with git + parse safety.

    Modes (selected by `action`):
      edit (default) — string replacement(s) in an existing file.
        Provide either (old_string, new_string) for a single edit, or
        a list `edits=[{old_string, new_string, replace_all?}, ...]`
        for atomic multi-edit. Each old_string must be unique unless
        replace_all is true.
      create — write a new file. Requires `file` and `new_string`
        (the full file contents). Errors if the file already exists.
      delete — remove an existing file. Requires `file`.

    All actions: checkpoint commit before, parse-verify after, rollback
    on failure, commit on success, refresh the state-dir copy. Returns
    triggers an auto-restart so changes load.
    """
    pkg_dir = Path(__file__).resolve().parent

    if action not in ("edit", "create", "delete"):
        return {"content": f"Unknown action: {action}", "is_error": True}

    # ---- create -------------------------------------------------------
    if action == "create":
        if not file:
            return {"content": "create requires file=", "is_error": True}
        if not new_string:
            return {"content": "create requires new_string (file contents)",
                    "is_error": True}
        target = pkg_dir / file
        if target.exists():
            return {"content": f"File already exists: {file}. Use action='edit'.",
                    "is_error": True}
        # Defense against escapes
        try:
            target.resolve().relative_to(pkg_dir.resolve())
        except ValueError:
            return {"content": "file must be inside the package",
                    "is_error": True}

        parse_err = _verify_parse(target, new_string)
        if parse_err:
            return {"content": f"Refused — parse error in new file:\n{parse_err}",
                    "is_error": True}

        _git_run(["add", "-A"])
        _git_run(["commit", "--allow-empty", "-m",
                  f"Pre-create checkpoint (before: {commit_message})"])

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_string)

        _git_run(["add", str(target)])
        result = _git_run(["commit", "-m",
                           f"Claude self-edit: {commit_message}"])
        rel = target.relative_to(pkg_dir)
        logger.info(f"Harness file created ({rel}): {commit_message}")
        try:
            state._copy_harness_source()
        except Exception:
            pass
        return {"content":
                f"Created {rel} ({len(new_string)} chars) and committed.\n"
                f"Git: {result.stdout.strip()}\n"
                f"The event loop will auto-restart to load your changes."}

    # ---- delete -------------------------------------------------------
    if action == "delete":
        if not file:
            return {"content": "delete requires file=", "is_error": True}
        target = pkg_dir / file
        if not target.exists():
            return {"content": f"File not found: {file}", "is_error": True}
        try:
            target.resolve().relative_to(pkg_dir.resolve())
        except ValueError:
            return {"content": "file must be inside the package",
                    "is_error": True}
        # Refuse to delete obvious load-bearing files
        rel = target.relative_to(pkg_dir).as_posix()
        if rel in ("__init__.py", "__main__.py", "config.py"):
            return {"content":
                    f"Refused — {rel} is load-bearing. Edit instead.",
                    "is_error": True}

        _git_run(["add", "-A"])
        _git_run(["commit", "--allow-empty", "-m",
                  f"Pre-delete checkpoint (before: {commit_message})"])

        target.unlink()
        _git_run(["add", "-A"])
        result = _git_run(["commit", "-m",
                           f"Claude self-edit: {commit_message}"])
        logger.info(f"Harness file deleted ({rel}): {commit_message}")
        try:
            state._copy_harness_source()
        except Exception:
            pass
        return {"content":
                f"Deleted {rel} and committed.\n"
                f"Git: {result.stdout.strip()}\n"
                f"The event loop will auto-restart to load your changes."}

    # ---- edit (single or multi) --------------------------------------
    if edits is None:
        if not old_string:
            return {"content":
                    "edit requires old_string + new_string, or edits=[...].",
                    "is_error": True}
        if old_string == new_string:
            return {"content": "old_string and new_string are identical.",
                    "is_error": True}
        edits = [{"old_string": old_string, "new_string": new_string,
                  "replace_all": False}]
    if not isinstance(edits, list) or not edits:
        return {"content": "edits must be a non-empty list",
                "is_error": True}

    # Determine target file
    if file:
        target = pkg_dir / file
        if not target.exists():
            return {"content": f"File not found: {file}. "
                    f"Use action='create' to create a new file.",
                    "is_error": True}
    else:
        first_old = edits[0].get("old_string", "")
        if not first_old:
            return {"content":
                    "edits[0].old_string is empty — cannot auto-detect file",
                    "is_error": True}
        candidates = []
        for py_file in pkg_dir.rglob("*.py"):
            try:
                source = py_file.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            count = source.count(first_old)
            if count > 0:
                candidates.append((py_file, count))
        if not candidates:
            return {"content":
                    "edits[0].old_string not found in any harness source file.",
                    "is_error": True}
        if len(candidates) > 1:
            files_list = ", ".join(
                str(f.relative_to(pkg_dir)) for f, _ in candidates)
            return {"content": f"edits[0].old_string found in multiple files: "
                    f"{files_list}. Specify file= to disambiguate.",
                    "is_error": True}
        target, _ = candidates[0]

    try:
        original = target.read_text()
        content = original
        applied = 0
        total_replacements = 0
        for idx, ed in enumerate(edits):
            old = ed.get("old_string", "")
            new = ed.get("new_string", "")
            replace_all = bool(ed.get("replace_all", False))
            if not old:
                return {"content":
                        f"edits[{idx}].old_string must be non-empty",
                        "is_error": True}
            if old == new:
                return {"content":
                        f"edits[{idx}]: old_string and new_string identical",
                        "is_error": True}
            occurrences = content.count(old)
            if occurrences == 0:
                return {"content":
                        f"edits[{idx}]: old_string not found "
                        f"(after {applied} prior edit(s) — batch rolled back)",
                        "is_error": True}
            if occurrences > 1 and not replace_all:
                return {"content":
                        f"edits[{idx}]: old_string found {occurrences} times "
                        f"in {target.relative_to(pkg_dir)} — must be unique "
                        f"or pass replace_all=true. (batch rolled back)",
                        "is_error": True}
            content = (content.replace(old, new) if replace_all
                       else content.replace(old, new, 1))
            total_replacements += (occurrences if replace_all else 1)
            applied += 1

        # Git checkpoint
        _git_run(["add", "-A"])
        _git_run(["commit", "--allow-empty", "-m",
                  f"Pre-edit checkpoint (before: {commit_message})"])

        target.write_text(content)

        parse_err = _verify_parse(target, content)
        if parse_err:
            target.write_text(original)
            logger.warning(
                f"Harness edit rolled back — parse failed: {parse_err}")
            return {"content":
                    f"Edit rolled back — parse error:\n{parse_err}",
                    "is_error": True}

        _git_run(["add", str(target)])
        result = _git_run(["commit", "-m",
                           f"Claude self-edit: {commit_message}"])

        rel_path = target.relative_to(pkg_dir)
        logger.info(
            f"Harness edited ({rel_path}): {applied} edit(s), "
            f"{total_replacements} replacement(s) — {commit_message}")

        try:
            state._copy_harness_source()
        except Exception:
            pass

        return {"content": (
            f"Edit applied to {rel_path} "
            f"({applied} edit(s), {total_replacements} replacement(s)) "
            f"and committed.\nGit: {result.stdout.strip()}\n"
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
        from claude_resident.state import StateManager
        state = StateManager(state_dir)
        state.append_file("scratchpad.md",
            f"\n\n---\n[SUPERVISOR {datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass
