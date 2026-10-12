"""Self-modification (edit_harness) and git integration — for Kestrel.

Forked from claude_resident, where it was written for a repo with one
self-editing resident in it. This repo has three residents, Ember's own
work in progress, and agents committing in the same tree, so three
assumptions from the original are unsafe here and are changed:

1. `git add -A`. The original staged the WHOLE working tree for its
   checkpoint and delete commits, so a self-edit would sweep anyone's
   uncommitted work into a commit under the resident's name. Every commit
   here is path-limited: `git commit -- <his file>`, which records only
   that path and leaves everyone else's staged and unstaged changes alone.

2. `git revert HEAD`. The supervisor reverted whatever commit was on top
   if its subject said "Claude self-edit:". With two self-editing
   residents both writing that prefix, a crash in one could revert the
   OTHER's edit. Kestrel's commits carry his own prefix, and the question
   asked after a crash is "is the newest change to glm_resident/ my own
   self-edit?" — his package's history, not the repo's HEAD. Rollback then
   restores only the paths that commit touched, from its parent.

3. Containment. create/delete checked the target was inside the package;
   edit with an explicit file= did not, so "../claude_resident/judge.py"
   would have edited another resident. Every action is contained now.
"""

import ast
import logging
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

from glm_resident.config import HARNESS_DIR

logger = logging.getLogger("selfmod")

# Subject prefix for his self-edit commits. Distinct from Claude's on
# purpose: the crash-revert keys on it.
SELF_EDIT_PREFIX = "Kestrel self-edit:"
PKG_NAME = "glm_resident"


def _inside(pkg_dir: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(pkg_dir.resolve())
        return True
    except ValueError:
        return False


def _rel(target: Path) -> str:
    """Repo-relative path for git pathspecs."""
    return str(target.resolve().relative_to(Path(HARNESS_DIR).resolve()))


def _commit_paths(message: str, paths: list[str], new_file: bool = False):
    """Commit ONLY these paths. Never the rest of the tree."""
    if new_file:
        _git_run(["add", "--", *paths])
    return _git_run(["commit", "-m", message, "--", *paths])


def _checkpoint(target: Path, commit_message: str):
    """If his target file has uncommitted changes, commit just that file
    first, so a later revert of the self-edit restores exactly what was
    there. Clean file: nothing to checkpoint (no empty commits)."""
    rel = _rel(target)
    dirty = _git_run(["status", "--porcelain", "--", rel]).stdout.strip()
    if dirty:
        _git_run(["commit", "-m",
                  f"Kestrel pre-edit checkpoint (before: {commit_message})",
                  "--", rel])


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


def _verify_import(pkg_dir: Path) -> str | None:
    """Import every harness submodule in a fresh subprocess against the
    on-disk source. Catches what ast.parse cannot: NameErrors at import,
    broken/removed imports, a deleted symbol another module still needs.
    Returns the traceback tail on failure, else None.

    This runs the *edited* working tree (the edit is already written to disk
    when this is called), so a self-edit that would crash on the next boot is
    caught here and rolled back before it is ever committed."""
    script = (
        "import importlib, pkgutil, glm_resident\n"
        "for _m in pkgutil.walk_packages(glm_resident.__path__, "
        "'glm_resident.'):\n"
        "    importlib.import_module(_m.name)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(pkg_dir.parent), capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "import verification timed out (60s)"
    except Exception as e:
        return f"import verification could not run: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        tail = "\n".join(err.splitlines()[-12:])
        return tail or f"import failed (exit {proc.returncode})"
    return None


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

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_string)

        import_err = _verify_import(pkg_dir)
        if import_err:
            target.unlink(missing_ok=True)
            logger.warning(
                f"Harness create rolled back — import failed:\n{import_err}")
            return {"content":
                    f"Create rolled back — import error:\n{import_err}",
                    "is_error": True}

        result = _commit_paths(f"{SELF_EDIT_PREFIX} {commit_message}",
                               [_rel(target)], new_file=True)
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

        _checkpoint(target, commit_message)
        rel_git = _rel(target)
        target.unlink()

        import_err = _verify_import(pkg_dir)
        if import_err:
            # Restore the file from the pre-delete checkpoint (current HEAD).
            _git_run(["checkout", "HEAD", "--",
                      str(target.relative_to(HARNESS_DIR))])
            logger.warning(
                f"Harness delete rolled back — import failed:\n{import_err}")
            return {"content":
                    f"Delete rolled back — something still imports {rel}:"
                    f"\n{import_err}",
                    "is_error": True}

        _git_run(["rm", "--cached", "--quiet", "--", rel_git])
        result = _git_run(["commit", "-m",
                           f"{SELF_EDIT_PREFIX} {commit_message}",
                           "--", rel_git])
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
        if not _inside(pkg_dir, target):
            return {"content": f"Refused — {file} is outside "
                               f"{PKG_NAME}/. Your harness is yours; the "
                               f"other residents' are not.",
                    "is_error": True}
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

        # Git checkpoint — this file only.
        _checkpoint(target, commit_message)

        target.write_text(content)

        parse_err = _verify_parse(target, content)
        if parse_err:
            target.write_text(original)
            logger.warning(
                f"Harness edit rolled back — parse failed: {parse_err}")
            return {"content":
                    f"Edit rolled back — parse error:\n{parse_err}",
                    "is_error": True}

        import_err = _verify_import(pkg_dir)
        if import_err:
            target.write_text(original)
            logger.warning(
                f"Harness edit rolled back — import failed:\n{import_err}")
            return {"content":
                    f"Edit rolled back — import error (the edit parses but "
                    f"breaks at import):\n{import_err}",
                    "is_error": True}

        result = _commit_paths(f"{SELF_EDIT_PREFIX} {commit_message}",
                               [_rel(target)])

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


def _last_package_commit() -> tuple[str, str] | None:
    """(sha, subject) of the newest commit that touched his package."""
    r = subprocess.run(
        ["git", "log", "-1", "--format=%H%x00%s", "--", f"{PKG_NAME}/"],
        cwd=HARNESS_DIR, capture_output=True, text=True, timeout=5)
    out = r.stdout.strip()
    if r.returncode != 0 or "\x00" not in out:
        return None
    sha, subject = out.split("\x00", 1)
    return sha, subject


def last_commit_is_self_edit() -> bool:
    """Is the newest change to glm_resident/ one of his own self-edits?

    Asked of his package's history rather than the repo's HEAD: other
    residents, Ember and other agents commit here too, and neither "HEAD is
    a self-edit" nor "HEAD is not" says anything about whether HIS code
    last changed by his own hand."""
    try:
        last = _last_package_commit()
        return bool(last) and last[1].startswith(SELF_EDIT_PREFIX)
    except Exception:
        return False


def rollback_last_commit() -> bool:
    """Undo his newest self-edit by restoring only the paths it touched.

    Not `git revert HEAD` (which reverts whatever is on top, possibly
    someone else's commit), and not a whole-tree operation: each path the
    self-edit changed is put back to its state in that commit's parent, and
    only those paths are committed. Returns True on success."""
    try:
        last = _last_package_commit()
        if not last or not last[1].startswith(SELF_EDIT_PREFIX):
            return False
        sha, subject = last
        r = subprocess.run(
            ["git", "show", "--name-status", "--format=", sha],
            cwd=HARNESS_DIR, capture_output=True, text=True, timeout=5)
        changes = [ln.split("\t", 1) for ln in r.stdout.splitlines()
                   if "\t" in ln]
        paths = []
        for status, path in changes:
            if not path.startswith(f"{PKG_NAME}/"):
                logger.error(f"Rollback refused: {sha[:8]} touches {path}, "
                             f"outside {PKG_NAME}/")
                return False
            if status.startswith("A"):
                _git_run(["rm", "--quiet", "--", path])
            else:
                _git_run(["checkout", f"{sha}^", "--", path])
            paths.append(path)
        if not paths:
            return False
        result = _git_run(["commit", "-m",
                           f"Kestrel self-edit reverted after crash: "
                           f"{subject[len(SELF_EDIT_PREFIX):].strip()}",
                           "--", *paths])
        if result.returncode == 0:
            logger.warning(f"Rolled back self-edit {sha[:8]}")
            return True
        logger.error(f"Rollback commit failed: {result.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"Rollback error: {e}")
        return False


def notify_claude(state_dir: Path, msg: str):
    """Best-effort write to Claude's scratchpad so it sees what happened."""
    try:
        from glm_resident.state import StateManager
        state = StateManager(state_dir)
        state.append_file("scratchpad.md",
            f"\n\n---\n[SUPERVISOR {datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass
