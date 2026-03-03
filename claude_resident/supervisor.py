"""Supervisor process — crash loop detection and rollback."""

import sys
import time
import logging
import subprocess

from claude_resident.config import HARNESS_DIR
from claude_resident.selfmod import (
    last_commit_is_self_edit,
    rollback_last_commit,
    notify_claude,
)

logger = logging.getLogger("supervisor")

CRASH_WINDOW = 60
MAX_CRASHES = 3


def run_supervised(child_cmd: list[str], state_dir):
    """Supervisor loop: launch event loop as subprocess, handle crashes."""
    logger.info("Supervisor starting")

    crash_times: list[float] = []

    while True:
        logger.info("Supervisor: launching event loop")
        try:
            rc = subprocess.call(child_cmd)
        except KeyboardInterrupt:
            logger.info("Supervisor: interrupted, shutting down")
            break

        if rc == 0:
            logger.info("Supervisor: clean shutdown")
            break

        if rc == 42:
            logger.info("Supervisor: self-edit restart (exit 42)")
            crash_times.clear()
            continue

        logger.error(f"Supervisor: child exited with code {rc}")

        if last_commit_is_self_edit():
            logger.warning(
                "Supervisor: crash after self-edit — rolling back")
            rollback_last_commit()
            notify_claude(state_dir,
                f"My self-edit caused a runtime crash (exit code {rc}) "
                f"and was auto-reverted by the supervisor.")
            crash_times.clear()
            logger.info("Supervisor: restarting after rollback")
            continue

        now = time.time()
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW]
        crash_times.append(now)
        if len(crash_times) >= MAX_CRASHES:
            logger.error(
                f"Supervisor: {MAX_CRASHES} crashes in {CRASH_WINDOW}s "
                "— giving up")
            notify_claude(state_dir,
                f"Event loop crash-looped ({MAX_CRASHES}x in "
                f"{CRASH_WINDOW}s, last exit code {rc}). "
                "Sysadmin intervention needed.")
            break

        logger.info(
            f"Supervisor: restarting after crash "
            f"({len(crash_times)}/{MAX_CRASHES} in window)")
        time.sleep(3)
