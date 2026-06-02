"""Sandbox file tools: get and upload files from the sandbox workspace."""

import base64
import logging
from pathlib import Path

logger = logging.getLogger("tools.sandbox")


def get_sandbox_file(inp: dict, sandbox_dir: str | None) -> dict:
    path = inp.get("path", "")
    if not path or ".." in path or path.startswith("/"):
        return {"content": "Invalid path.", "is_error": True}
    if sandbox_dir is None:
        return {"content": "No sandbox workspace active. Run run_sandbox first.",
                "is_error": True}

    filepath = Path(sandbox_dir) / path
    if not filepath.exists():
        return {"content": f"File not found: {path}", "is_error": True}

    binary_extensions = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".pdf", ".bin", ".dat"
    }
    if filepath.suffix.lower() in binary_extensions:
        data = filepath.read_bytes()
        if len(data) > 5_000_000:
            return {"content": f"File too large ({len(data)} bytes). "
                    "Upload instead of reading.", "is_error": True}
        b64 = base64.b64encode(data).decode("ascii")
        return {"content": f"[binary file: {path}, {len(data)} bytes]\n"
                f"base64: {b64[:200]}... ({len(b64)} chars total)"}

    content = filepath.read_text(errors="replace")
    if len(content) > 50_000:
        content = (content[:50_000] +
                   f"\n\n[Truncated — file is {len(content)} chars total]")
    return {"content": content}


def upload_sandbox_file(inp: dict, sandbox_dir: str | None,
                        zulip_client) -> dict:
    path = inp.get("path", "")
    if not path or ".." in path or path.startswith("/"):
        return {"content": "Invalid path.", "is_error": True}
    if sandbox_dir is None:
        return {"content": "No sandbox workspace active. Run run_sandbox first.",
                "is_error": True}

    filepath = Path(sandbox_dir) / path
    if not filepath.exists():
        return {"content": f"File not found: {path}", "is_error": True}

    try:
        with open(filepath, "rb") as f:
            result = zulip_client.upload_file(f)

        if result.get("result") == "success":
            uri = result["uri"]
            logger.info(f"Sandbox: uploaded {path} -> {uri}")
            return {"content": f"Uploaded successfully. "
                    f"Use this in your message: [{filepath.name}]({uri})"}
        else:
            return {"content": f"Upload failed: {result.get('msg', 'unknown')}",
                    "is_error": True}

    except Exception as e:
        return {"content": f"Upload error: {e}", "is_error": True}
