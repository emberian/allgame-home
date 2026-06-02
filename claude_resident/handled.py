"""Durable ledger of message ids the resident has already handled.

Survives harness restarts so neither `replay_pending` nor `handle_message`
re-responds to a message that was already processed before a relaunch.

Why this exists: the positional "was Claude the last speaker?" heuristic in
`replay_pending` is fragile. A restart mid-response, a reply routed to a DM
or a different topic, or a reaction landing after Claude's message can all
leave a human as the apparent "last speaker" on a message Claude already
answered — so replay re-responds to it. This ledger is the durable source of
truth for "already handled," independent of who spoke last.

Append-only (one id per line) for crash-safety: marking is a single append,
never a read-modify-write, so an interrupted write can't corrupt the set.
The file is compacted to the most-recent `cap` ids on load when it grows
large; replay only ever inspects the recent tail, so evicting ancient ids is
safe.
"""

import logging
from collections import deque

logger = logging.getLogger("handled")

LEDGER_FILE = "handled_message_ids.log"


class HandledSet:
    """A persisted set of handled message ids with bounded memory."""

    def __init__(self, state, cap: int = 5000):
        self.state = state
        self.cap = cap
        self._set: set[int] = set()
        self._order: deque[int] = deque()
        self._load()

    def _load(self):
        raw = self.state.read_file(LEDGER_FILE) or ""
        ids: list[int] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ids.append(int(line))
            except ValueError:
                continue
        # Keep only the most recent `cap`, and compact the file if it grew.
        if len(ids) > self.cap:
            ids = ids[-self.cap:]
            try:
                self.state.write_file(
                    LEDGER_FILE, "\n".join(str(i) for i in ids) + "\n")
            except Exception as e:
                logger.warning(f"Failed to compact handled ledger: {e}")
        for i in ids:
            if i not in self._set:
                self._set.add(i)
                self._order.append(i)
        logger.info(f"Loaded {len(self._set)} handled message ids")

    def __contains__(self, msg_id) -> bool:
        try:
            return int(msg_id) in self._set
        except (TypeError, ValueError):
            return False

    def mark(self, msg_id):
        """Record a message id as handled (idempotent, persisted immediately)."""
        try:
            mid = int(msg_id)
        except (TypeError, ValueError):
            return
        if mid in self._set:
            return
        self._set.add(mid)
        self._order.append(mid)
        try:
            self.state.append_file(LEDGER_FILE, f"{mid}\n")
        except Exception as e:
            logger.warning(f"Failed to persist handled id {mid}: {e}")
        # Evict oldest from memory if oversized; the file compacts on next load.
        while len(self._order) > self.cap:
            old = self._order.popleft()
            self._set.discard(old)
