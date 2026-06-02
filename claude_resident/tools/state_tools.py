"""State file tools: read, write, edit, list, glob, grep."""

import base64
import fnmatch
import logging
import re

from claude_resident.util import zulip_upload_file

logger = logging.getLogger("tools.state")

# Files where 'replace' and surgical edits are blocked at the tool layer.
# Currently empty: even the journal can be edited (the model has the judgment
# to trim history thoughtfully). Add filenames here if a file ever needs to
# become tool-immutable.
APPEND_ONLY_FILES: set[str] = set()

# Hard cap on glob results to keep tool output bounded
GLOB_MAX_RESULTS = 200

# Grep limits
GREP_MAX_FILES = 500           # max files scanned per call
GREP_MAX_MATCHES = 200         # max match lines returned
GREP_MAX_FILE_BYTES = 2_000_000  # skip files larger than this
# Default skip pattern — chat logs are huge and rarely useful for state grep.
# Callers can pass include="channels/**/*.jsonl" explicitly to include them.
GREP_DEFAULT_SKIP_GLOBS = ("channels/**/*.jsonl",)


def _format_with_line_numbers(lines: list[str], start_line: int = 1) -> str:
    """cat -n style: '<line>\t<content>'. Matches Claude Code's Read output
    so the model can reference precise line numbers when crafting edits."""
    width = max(4, len(str(start_line + len(lines) - 1)))
    return "\n".join(
        f"{str(start_line + i).rjust(width)}\t{line}"
        for i, line in enumerate(lines)
    )


def read_state_file(inp: dict, state) -> dict:
    path = inp.get("path", "")
    offset = inp.get("offset", 0)       # line offset (0-based)
    limit = inp.get("limit", 0)         # max lines (0 = all)
    raw = bool(inp.get("raw", False))   # opt-out of line numbering

    if ".." in path or path.startswith("/"):
        return {"content": "Invalid path: must be relative, no '..'",
                "is_error": True}
    full_path = state.root / path
    if not full_path.exists():
        return {"content": f"File not found: {path}", "is_error": True}
    try:
        content = full_path.read_text()
    except (UnicodeDecodeError, ValueError):
        rawbytes = full_path.read_bytes()
        b64 = base64.b64encode(rawbytes).decode("ascii")
        logger.debug(f"Tool read (binary/base64): {path} ({len(rawbytes)} bytes)")
        return {"content":
                f"[binary file: {len(rawbytes)} bytes, base64-encoded]\n{b64}"}

    total_chars = len(content)
    all_lines = content.split("\n")
    # Don't count a trailing-newline-induced empty element as a line
    if all_lines and all_lines[-1] == "":
        all_lines = all_lines[:-1]
    total_lines = len(all_lines)

    start = max(0, offset)
    end = start + limit if limit > 0 else total_lines
    sliced = all_lines[start:end]

    paginated = offset > 0 or limit > 0
    if raw:
        body = "\n".join(sliced)
    else:
        body = _format_with_line_numbers(sliced, start_line=start + 1)

    if paginated:
        header = (f"[lines {start+1}-{start+len(sliced)} of "
                  f"{total_lines} total, {total_chars} chars in file]")
        body = header + "\n" + body

    if len(body) > 50_000:
        body = (body[:50_000] +
                f"\n\n[Truncated at 50K chars — file is {total_chars} "
                f"chars / {total_lines} lines total. "
                f"Use offset/limit to paginate.]")
    logger.debug(f"Tool read: {path} ({len(body)} chars)")
    return {"content": body}


def write_state_file(inp: dict, state) -> dict:
    path = inp.get("path", "")
    content = inp.get("content", "")
    action = inp.get("action", "replace")
    if ".." in path or path.startswith("/"):
        return {"content": "Invalid path", "is_error": True}
    if action not in ("replace", "append"):
        return {"content": f"Invalid action: {action}", "is_error": True}
    basename = path.split("/")[-1] if "/" in path else path
    if basename in APPEND_ONLY_FILES and action == "replace":
        return {"content": f"Blocked: {path} is append-only by design. "
                "Use action='append'.", "is_error": True}
    logger.info(f"Tool state write: {action} {path} ({len(content)} chars)")
    previous = None
    if action == "replace":
        previous = state.read_file(path)
        state.write_file(path, content)
    else:
        state.append_file(path, content)
    result = f"OK: {action} {path} ({len(content)} chars written)"
    if previous is not None:
        preview = previous[:500]
        if len(previous) > 500:
            preview += f"\n... [{len(previous)} chars total]"
        result += f"\n\n[Previous content was:]\n{preview}"
    return {"content": result}


def list_state_files(inp: dict, state) -> dict:
    directory = inp.get("directory", "")
    if ".." in directory:
        return {"content": "Invalid path", "is_error": True}
    target = state.root / directory
    if not target.exists() or not target.is_dir():
        return {"content": f"Directory not found: {directory or '(root)'}",
                "is_error": True}
    entries = []
    for item in sorted(target.iterdir()):
        if item.is_dir():
            entries.append(f"  {item.name}/")
        else:
            size = item.stat().st_size
            entries.append(f"  {item.name} ({size} bytes)")
    return {"content": "\n".join(entries) if entries else "(empty directory)"}


def edit_state_file(inp: dict, state) -> dict:
    """Surgical string replacement(s) in a state file.

    Two modes:
      Single: pass old_string + new_string (+ optional replace_all).
      Batch:  pass edits=[{old_string, new_string, replace_all?}, ...].
              Edits apply sequentially to the same file. If any one fails
              the whole batch is rolled back — no partial writes.

    Cheaper than write_state_file for incremental tweaks. The file must
    already exist.
    """
    path = inp.get("path", "")
    if ".." in path or path.startswith("/"):
        return {"content": "Invalid path", "is_error": True}

    edits = inp.get("edits")
    if edits is None:
        edits = [{
            "old_string": inp.get("old_string", ""),
            "new_string": inp.get("new_string", ""),
            "replace_all": bool(inp.get("replace_all", False)),
        }]
    if not isinstance(edits, list) or not edits:
        return {"content": "edits must be a non-empty list "
                "(or pass old_string/new_string directly).",
                "is_error": True}

    basename = path.split("/")[-1] if "/" in path else path
    if basename in APPEND_ONLY_FILES:
        return {"content": f"Blocked: {path} is append-only by design.",
                "is_error": True}

    full_path = state.root / path
    if not full_path.exists():
        return {"content": f"File not found: {path}. Use write_state_file to "
                f"create it.", "is_error": True}

    try:
        content = full_path.read_text()
    except (UnicodeDecodeError, ValueError):
        return {"content": f"Cannot edit binary file: {path}",
                "is_error": True}

    original = content
    applied = 0
    total_replacements = 0
    for idx, ed in enumerate(edits):
        old = ed.get("old_string", "")
        new = ed.get("new_string", "")
        replace_all = bool(ed.get("replace_all", False))

        if not old:
            return {"content":
                    f"edit[{idx}]: old_string must be non-empty",
                    "is_error": True}
        if old == new:
            return {"content":
                    f"edit[{idx}]: old_string and new_string are identical",
                    "is_error": True}

        occurrences = content.count(old)
        if occurrences == 0:
            return {"content":
                    f"edit[{idx}]: old_string not found in {path} "
                    f"(after {applied} prior edit(s) applied — "
                    f"batch rolled back)",
                    "is_error": True}
        if occurrences > 1 and not replace_all:
            return {"content":
                    f"edit[{idx}]: old_string matches {occurrences} times. "
                    f"Provide a unique substring or pass replace_all=true. "
                    f"(batch rolled back)",
                    "is_error": True}

        n = occurrences if replace_all else 1
        content = (content.replace(old, new) if replace_all
                   else content.replace(old, new, 1))
        total_replacements += n
        applied += 1

    state.write_file(path, content)
    delta = len(content) - len(original)
    sign = "+" if delta >= 0 else ""
    logger.info(
        f"Tool state edit: {path} "
        f"({applied} edit(s), {total_replacements} replacement(s), "
        f"{sign}{delta} chars)")
    return {"content":
            f"OK: edited {path} "
            f"({applied} edit(s), {total_replacements} replacement(s), "
            f"{sign}{delta} chars)"}


def upload_state_file(inp: dict, state, zulip_client) -> dict:
    """Upload a file from the state directory to Zulip.

    Returns a markdown link Claude can paste into send_message. Useful for
    sharing journal excerpts, generated content, transcripts, or any file
    he's curated in his state dir. For files produced in the sandbox, use
    upload_sandbox_file instead.
    """
    path = inp.get("path", "")
    if not path or ".." in path or path.startswith("/"):
        return {"content": "Invalid path: must be relative, no '..'",
                "is_error": True}

    full_path = state.root / path
    if not full_path.exists():
        return {"content": f"File not found: {path}", "is_error": True}
    if not full_path.is_file():
        return {"content": f"Not a file: {path}", "is_error": True}

    # Cap upload size at 20MB (Zulip default limit is 25MB, leave headroom)
    size = full_path.stat().st_size
    if size > 20 * 1024 * 1024:
        return {"content":
                f"File too large ({size} bytes, max 20 MB). "
                f"Split or compress first.", "is_error": True}
    if size == 0:
        return {"content": f"File is empty: {path}", "is_error": True}

    try:
        result = zulip_upload_file(full_path, zulip_client)
        if result.get("result") == "success":
            uri = result["uri"]
            logger.info(
                f"State upload: {path} -> {uri} ({size} bytes)")
            return {"content":
                    f"Uploaded {path} ({size} bytes). "
                    f"Use this in your message: "
                    f"[{full_path.name}]({uri})"}
        return {"content":
                f"Upload failed: {result.get('msg', 'unknown')}",
                "is_error": True}
    except Exception as e:
        return {"content": f"Upload error: {e}", "is_error": True}


def glob_state_files(inp: dict, state) -> dict:
    """Recursive glob across the state directory.

    Pattern is a shell-style glob applied to paths relative to the state root
    (e.g. 'people/*.md', 'channels/*/*.jsonl', '**/*.md', 'allgame/*').
    pathlib's '**' matches any number of directories. Returns matching file
    paths with sizes, optionally filtered to a starting subdirectory.
    """
    pattern = inp.get("pattern", "").strip()
    base = inp.get("directory", "").strip() or ""

    if ".." in pattern or ".." in base:
        return {"content": "Invalid path", "is_error": True}
    if not pattern:
        return {"content": "pattern is required (e.g. '**/*.md', "
                "'people/*.md', 'channels/*/*.jsonl')",
                "is_error": True}

    target = state.root / base if base else state.root
    if not target.exists() or not target.is_dir():
        return {"content": f"Directory not found: {base or '(root)'}",
                "is_error": True}

    matches: list[tuple[str, int]] = []
    try:
        for path in target.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(state.root).as_posix()
            matches.append((rel, path.stat().st_size))
            if len(matches) >= GLOB_MAX_RESULTS:
                break
    except (ValueError, OSError) as e:
        return {"content": f"Glob error: {e}", "is_error": True}

    matches.sort()
    if not matches:
        return {"content": f"No matches for {pattern!r}"
                + (f" under {base}" if base else "")}
    lines = [f"  {rel} ({size} bytes)" for rel, size in matches]
    header = (f"{len(matches)} match(es) for {pattern!r}"
              + (f" under {base}" if base else "")
              + (" [capped]" if len(matches) >= GLOB_MAX_RESULTS else ""))
    return {"content": header + "\n" + "\n".join(lines)}


def grep_state(inp: dict, state) -> dict:
    """Regex content search across state files.

    Pattern is a Python regex. Modes:
      content (default) — lines with file:line:text
      files_with_matches — just paths
      count — path with hit count

    Filter to specific files with include= (glob) and/or directory= (subdir).
    Set case_insensitive=true for ignore-case. Channel jsonl logs are skipped
    by default — pass include='channels/**/*.jsonl' to scan them.
    """
    pattern = inp.get("pattern", "")
    include = inp.get("include", "").strip()
    base = inp.get("directory", "").strip() or ""
    mode = inp.get("mode", "content")
    case_insensitive = bool(inp.get("case_insensitive", False))
    context_lines = max(0, min(5, int(inp.get("context", 0) or 0)))

    if not pattern:
        return {"content": "pattern is required", "is_error": True}
    if ".." in base or ".." in include:
        return {"content": "Invalid path", "is_error": True}
    if mode not in ("content", "files_with_matches", "count"):
        return {"content": f"Invalid mode: {mode}", "is_error": True}

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"content": f"Invalid regex: {e}", "is_error": True}

    target = state.root / base if base else state.root
    if not target.exists() or not target.is_dir():
        return {"content": f"Directory not found: {base or '(root)'}",
                "is_error": True}

    def _matches_skip(rel: str) -> bool:
        if include:
            return False  # explicit include overrides skip defaults
        return any(fnmatch.fnmatch(rel, g) for g in GREP_DEFAULT_SKIP_GLOBS)

    # Build the file list. With include= we honor the user pattern via
    # pathlib glob (which handles '**' natively); otherwise we walk all.
    files: list = []
    try:
        iterator = target.glob(include) if include else target.rglob("*")
        for path in iterator:
            if not path.is_file():
                continue
            rel = path.relative_to(state.root).as_posix()
            if _matches_skip(rel):
                continue
            try:
                if path.stat().st_size > GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
            if len(files) >= GREP_MAX_FILES:
                break
    except (ValueError, OSError) as e:
        return {"content": f"Include glob error: {e}", "is_error": True}

    files.sort()
    match_lines: list[str] = []
    per_file_counts: dict[str, int] = {}
    truncated = False

    for path in files:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, ValueError, OSError):
            continue
        rel = path.relative_to(state.root).as_posix()
        lines = text.split("\n")
        file_hits: list[int] = []
        for i, line in enumerate(lines):
            if regex.search(line):
                file_hits.append(i)
        if not file_hits:
            continue
        per_file_counts[rel] = len(file_hits)

        if mode == "files_with_matches" or mode == "count":
            continue

        # content mode — emit matched lines with optional context
        emitted_lines: set[int] = set()
        for li in file_hits:
            lo = max(0, li - context_lines)
            hi = min(len(lines) - 1, li + context_lines)
            for k in range(lo, hi + 1):
                if k in emitted_lines:
                    continue
                emitted_lines.add(k)
                marker = ":" if k == li or context_lines == 0 else "-"
                match_lines.append(f"{rel}:{k+1}{marker}{lines[k]}")
                if len(match_lines) >= GREP_MAX_MATCHES:
                    truncated = True
                    break
            if truncated:
                break
            if context_lines > 0:
                match_lines.append("--")
        if truncated:
            break

    if not per_file_counts:
        return {"content": f"No matches for /{pattern}/"
                + (f" in {include}" if include else "")
                + (f" under {base}" if base else "")}

    if mode == "files_with_matches":
        body = "\n".join(sorted(per_file_counts.keys()))
        header = f"{len(per_file_counts)} file(s) match /{pattern}/"
    elif mode == "count":
        body = "\n".join(f"{p}: {c}"
                         for p, c in sorted(per_file_counts.items()))
        total = sum(per_file_counts.values())
        header = (f"{total} match(es) across "
                  f"{len(per_file_counts)} file(s) for /{pattern}/")
    else:
        body = "\n".join(match_lines)
        total = sum(per_file_counts.values())
        header = (f"{total} match(es) across "
                  f"{len(per_file_counts)} file(s) for /{pattern}/"
                  + (" [output capped]" if truncated else ""))

    return {"content": header + "\n" + body}
