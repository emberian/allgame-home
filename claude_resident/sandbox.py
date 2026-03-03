"""Docker sandbox and persistent background container management."""

import os
import shutil
import logging
import subprocess
import tempfile
from pathlib import Path

from claude_resident.config import (
    SANDBOX_IMAGE, SANDBOX_TIMEOUT, SANDBOX_MEMORY,
    BG_CONTAINER_NAME, BG_CONTAINER_MEMORY,
)

logger = logging.getLogger("sandbox")


def ensure_bg_container(state_root: Path) -> bool:
    """Start the persistent background container if not already running."""
    check = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", BG_CONTAINER_NAME],
        capture_output=True, text=True, timeout=10)
    if check.returncode == 0 and "true" in check.stdout.lower():
        return True

    subprocess.run(
        ["docker", "rm", "-f", BG_CONTAINER_NAME],
        capture_output=True, timeout=10)

    bg_workspace = state_root / "bg_workspace"
    bg_workspace.mkdir(exist_ok=True)

    result = subprocess.run([
        "docker", "run", "-d",
        "--name", BG_CONTAINER_NAME,
        "--memory", BG_CONTAINER_MEMORY,
        "--cpus", "4",
        "-e", f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
        "-e", f"KAGI_API_KEY={os.environ.get('KAGI_API_KEY', '')}",
        "-v", f"{bg_workspace}:/workspace",
        "-w", "/workspace",
        SANDBOX_IMAGE,
        "sleep", "infinity",
    ], capture_output=True, text=True, timeout=30)

    if result.returncode == 0:
        logger.info(f"Started persistent background container: {BG_CONTAINER_NAME}")
        return True
    else:
        logger.error(f"Failed to start bg container: {result.stderr}")
        return False


def stop_bg_container():
    """Stop and remove the persistent background container."""
    subprocess.run(
        ["docker", "rm", "-f", BG_CONTAINER_NAME],
        capture_output=True, timeout=10)
    logger.info("Stopped persistent background container")


def run_sandbox(command: str, files: dict[str, str],
                state_root: Path, sandbox_dir: str | None,
                state_files: dict[str, str] | None = None
                ) -> tuple[dict, str | None]:
    """Run a command in an ephemeral Docker sandbox.

    Returns (result_dict, sandbox_dir) where sandbox_dir is the workspace path
    to be reused across calls.
    """
    if not command:
        return {"content": "No command provided.", "is_error": True}, sandbox_dir

    if sandbox_dir is None:
        sandbox_dir = tempfile.mkdtemp(prefix="claude_sandbox_")
        logger.info(f"Sandbox: created workspace {sandbox_dir}")

    try:
        for name, file_content in files.items():
            safe_name = name.replace("..", "").lstrip("/")
            if not safe_name:
                continue
            filepath = Path(sandbox_dir) / safe_name
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(file_content)

        if state_files:
            for state_path, dest_name in state_files.items():
                if ".." in state_path or state_path.startswith("/"):
                    continue
                src = state_root / state_path
                if not src.exists():
                    continue
                safe_dest = dest_name.replace("..", "").lstrip("/")
                if not safe_dest:
                    continue
                dest = Path(sandbox_dir) / safe_dest
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dest))

        docker_cmd = [
            "docker", "run", "--rm",
            "--memory", SANDBOX_MEMORY,
            "--cpus", "2",
            "--pids-limit", "128",
            "-e", f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
            "-v", f"{sandbox_dir}:/workspace",
            "-v", f"{state_root}:/state:ro",
            "-w", "/workspace",
            SANDBOX_IMAGE,
            "/bin/bash", "-c", command,
        ]

        logger.info(f"Sandbox: running command ({len(files)} files provided)")
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=SANDBOX_TIMEOUT,
        )

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if not output:
            output = "(no output)"

        if result.returncode != 0:
            output += f"\n\n[exit code: {result.returncode}]"

        workspace_files = []
        for f in sorted(Path(sandbox_dir).rglob("*")):
            if f.is_file():
                rel = f.relative_to(sandbox_dir)
                size = f.stat().st_size
                workspace_files.append(f"  {rel} ({size} bytes)")
        if workspace_files:
            output += "\n\n--- workspace files ---\n" + "\n".join(workspace_files)

        if len(output) > 50_000:
            output = output[:50_000] + "\n\n[Output truncated at 50k chars]"

        logger.info(
            f"Sandbox: completed (exit {result.returncode}, "
            f"{len(output)} chars output)")
        return {"content": output}, sandbox_dir

    except subprocess.TimeoutExpired:
        logger.warning(f"Sandbox: command timed out after {SANDBOX_TIMEOUT}s")
        return {"content": f"Command timed out after {SANDBOX_TIMEOUT}s",
                "is_error": True}, sandbox_dir
    except FileNotFoundError:
        return {"content": "Docker not found. Is Docker installed and running?",
                "is_error": True}, sandbox_dir
    except Exception as e:
        logger.error(f"Sandbox error: {e}", exc_info=True)
        return {"content": f"Sandbox error: {e}", "is_error": True}, sandbox_dir


def run_background(command: str, files: dict[str, str],
                   state_root: Path, timeout: int = 120) -> dict:
    """Run a command in the persistent background container."""
    if not command:
        return {"content": "No command provided.", "is_error": True}

    if not ensure_bg_container(state_root):
        return {"content": "Failed to start background container.", "is_error": True}

    try:
        for name, content in files.items():
            safe_name = name.replace("..", "").lstrip("/")
            if not safe_name:
                continue
            write_cmd = [
                "docker", "exec", BG_CONTAINER_NAME,
                "bash", "-c",
                f"mkdir -p $(dirname '/workspace/{safe_name}') "
                f"&& cat > '/workspace/{safe_name}'"]
            subprocess.run(write_cmd, input=content, capture_output=True,
                           text=True, timeout=10)

        if timeout == 0:
            exec_cmd = ["docker", "exec", "-d", BG_CONTAINER_NAME,
                        "bash", "-c", command]
            subprocess.run(exec_cmd, capture_output=True, timeout=10)
            logger.info("Background: fire-and-forget command launched")
            return {"content": "Command launched in background (fire-and-forget)."}

        exec_cmd = ["docker", "exec", BG_CONTAINER_NAME,
                    "bash", "-c", command]
        result = subprocess.run(exec_cmd, capture_output=True, text=True,
                                timeout=timeout)

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if not output:
            output = "(no output)"
        if result.returncode != 0:
            output += f"\n\n[exit code: {result.returncode}]"
        if len(output) > 50_000:
            output = output[:50_000] + "\n\n[Output truncated at 50k chars]"

        logger.info(f"Background: command completed (exit {result.returncode})")
        return {"content": output}

    except subprocess.TimeoutExpired:
        return {"content": f"Command timed out after {timeout}s. "
                "Use timeout=0 for fire-and-forget.", "is_error": True}
    except Exception as e:
        return {"content": f"Background container error: {e}", "is_error": True}


def cleanup_sandbox(sandbox_dir: str | None):
    """Clean up ephemeral sandbox workspace."""
    if sandbox_dir:
        shutil.rmtree(sandbox_dir, ignore_errors=True)
        logger.debug("Sandbox: cleaned up workspace")
