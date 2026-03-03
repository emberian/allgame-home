"""State file tools: read, write, list, person notes."""

import base64
import logging

from claude_resident.util import safe_filename

logger = logging.getLogger("tools.state")

# Files where replace is blocked — append-only by architecture
APPEND_ONLY_FILES = {"journal.md"}


def read_state_file(inp: dict, state) -> dict:
    path = inp.get("path", "")
    if ".." in path or path.startswith("/"):
        return {"content": "Invalid path: must be relative, no '..'",
                "is_error": True}
    full_path = state.root / path
    if not full_path.exists():
        return {"content": f"File not found: {path}", "is_error": True}
    try:
        content = full_path.read_text()
        if len(content) > 50_000:
            content = (content[:50_000] +
                       f"\n\n[Truncated — file is {len(content)} chars total]")
        logger.debug(f"Tool read: {path} ({len(content)} chars)")
        return {"content": content}
    except (UnicodeDecodeError, ValueError):
        raw = full_path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        logger.debug(f"Tool read (binary/base64): {path} ({len(raw)} bytes)")
        return {"content": f"[binary file: {len(raw)} bytes, base64-encoded]\n{b64}"}


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


def get_person_notes(inp: dict, state) -> dict:
    name = inp["name"]
    sname = safe_filename(name)
    content = state.read_file(f"people/{sname}.md")
    if content is None:
        return {"content": f"No notes found for '{name}'. Create with "
                f"write_state_file('people/{sname}.md', ...)."}
    return {"content": content}
