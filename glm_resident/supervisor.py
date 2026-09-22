"""Supervisor process — crash loop detection and rollback."""

import os
import sys
import time
import logging
import threading
import subprocess

from glm_resident.config import HARNESS_DIR
from glm_resident.selfmod import (
    last_commit_is_self_edit,
    rollback_last_commit,
    notify_claude,
)

logger = logging.getLogger("supervisor")

CRASH_WINDOW = 60
MAX_CRASHES = 3
ORPHAN_CHECK_SECONDS = 10


def exit_if_orphaned(interval: float = ORPHAN_CHECK_SECONDS):
    """Make the supervised child die with its supervisor.

    Stopping the resident means killing the launcher, and the event-loop
    child would survive it — reparented to init, still holding a live Zulip
    event queue, still answering. On 2026-08-14 three of them were running
    at once in the same rooms, from restarts on three different days,
    talking over each other in #clankerville>tavern. Nothing upstream
    noticed, because every one of them was individually healthy: the only
    symptom was the room getting several replies to one message.

    A child that finds itself orphaned has no supervisor to restart it and
    no business speaking, so it leaves. os._exit because a duplicate voice
    in the room is worse than an orphaned sandbox container (the next boot
    recreates that anyway).
    """
    original = os.getppid()

    def _watch():
        while True:
            time.sleep(interval)
            now = os.getppid()
            if now != original or now == 1:
                logger.error(
                    "Supervisor gone (ppid %s -> %s) — exiting rather than "
                    "answer the room twice", original, now)
                os._exit(3)

    threading.Thread(target=_watch, daemon=True, name="orphan-watch").start()


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
            reverted = rollback_last_commit()
            if reverted:
                notify_claude(state_dir,
                    f"My self-edit caused a runtime crash (exit code {rc}) "
                    f"and was auto-reverted by the supervisor.")
                # Only a confirmed revert earns a clean slate. Otherwise fall
                # through to crash counting so a rollback that DIDN'T fix the
                # crash trips the breaker instead of looping forever.
                crash_times.clear()
                logger.info("Supervisor: restarting after rollback")
                continue
            logger.error(
                "Supervisor: rollback FAILED — counting this crash toward "
                "the breaker instead of blindly restarting")
            notify_claude(state_dir,
                f"My self-edit crashed (exit code {rc}) AND the auto-revert "
                f"failed. Supervisor is no longer trusting rollback; "
                f"sysadmin intervention likely needed.")

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
