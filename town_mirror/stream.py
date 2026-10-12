"""The live half of the mirror: the PDS firehose.

wss://pds.delve.town/xrpc/com.atproto.sync.subscribeRepos is public over
WebSocket (a plain HTTP GET says 401 AuthMissing, which is what made it look
closed at first). Each frame is two DAG-CBOR objects, a header and a body;
a #commit body carries its ops and a CAR of the blocks they point at.

The sequence number of every applied event is saved as `firehose_seq`, and
reconnects resume from it, so a restart neither drops nor double-applies
(applying is idempotent anyway). If the PDS no longer holds that far back
it says OutdatedCursor and the stream starts live; the crawler's reconcile
pass closes that gap.

An event the stream cannot fully apply (a commit marked tooBig, whose ops
are not inlined) leaves the repo's stored rev alone, so reconcile — which
re-crawls any repo whose rev moved — picks it up.
"""

import asyncio
import json
import logging
import time

import libipld
import websockets

from town_mirror import PDS
from town_mirror.crawl import (COLLECTIONS, PROFILE, _now, apply_record,
                                delete_record, forget_account)
from town_mirror.db import connect_rw

logger = logging.getLogger("town_mirror.stream")

URL = PDS.replace("https://", "wss://") + "/xrpc/com.atproto.sync.subscribeRepos"
COMMIT_EVERY_S = 1.0
TRACKED = set(COLLECTIONS) | {PROFILE}


def _state(db, key, value=None):
    if value is None:
        row = db.execute("SELECT value FROM crawl_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    db.execute("INSERT OR REPLACE INTO crawl_state VALUES (?, ?)", (key, str(value)))


class Stream:
    def __init__(self):
        self.db = connect_rw()
        self.applied = 0
        self.last_commit = time.monotonic()

    def _ensure_account(self, did: str, now: str):
        self.db.execute(
            "INSERT OR IGNORE INTO accounts (did, active, first_seen) VALUES (?, 1, ?)",
            (did, now))

    def handle(self, header: dict, body: dict):
        t = header.get("t")
        now = _now()
        if header.get("op") == -1:
            logger.warning(f"firehose error frame: {body}")
            return
        if t == "#info":
            logger.warning(f"firehose info: {body}")   # e.g. OutdatedCursor
            return
        seq = body.get("seq")
        did = body.get("repo") or body.get("did")
        if t == "#commit":
            self._ensure_account(did, now)
            if body.get("tooBig"):
                logger.info(f"tooBig commit from {did}; reconcile will fetch it")
            else:
                _, blocks = libipld.decode_car(body["blocks"]) if body.get("blocks") else (None, {})
                for op in body.get("ops") or []:
                    collection, _, rkey = op["path"].partition("/")
                    if collection not in TRACKED:
                        continue
                    uri = f"at://{did}/{op['path']}"
                    if op["action"] == "delete":
                        delete_record(self.db, collection, uri, now)
                        continue
                    cid = op.get("cid")
                    value = blocks.get(cid) if cid else None
                    if not isinstance(value, dict):
                        continue
                    apply_record(self.db, did, collection, uri,
                                 libipld.encode_cid(cid), value, now)
                    self.applied += 1
                self.db.execute("UPDATE accounts SET rev = ? WHERE did = ?",
                                (body.get("rev"), did))
        elif t == "#identity":
            self._ensure_account(did, now)
            if body.get("handle"):
                self.db.execute("UPDATE accounts SET handle = ? WHERE did = ?",
                                (body["handle"], did))
        elif t == "#account":
            status = body.get("status")
            if status == "deleted":
                forget_account(self.db, did)
            else:
                self.db.execute("UPDATE accounts SET active = ? WHERE did = ?",
                                (1 if body.get("active", True) else 0, did))
        if seq is not None:
            _state(self.db, "firehose_seq", seq)
            _state(self.db, "firehose_at", now)
        if time.monotonic() - self.last_commit >= COMMIT_EVERY_S:
            self.db.commit()
            self.last_commit = time.monotonic()

    async def run(self):
        backoff = 1
        while True:
            cursor = _state(self.db, "firehose_seq")
            url = URL + (f"?cursor={cursor}" if cursor else "")
            try:
                async with websockets.connect(url, open_timeout=30, max_size=None,
                                              ping_interval=30) as ws:
                    logger.info(f"firehose connected (cursor={cursor})")
                    backoff = 1
                    async for frame in ws:
                        try:
                            header, body = libipld.decode_dag_cbor_multi(frame)
                            self.handle(header, body)
                        except Exception as e:
                            logger.warning(f"bad frame: {e}")
            except Exception as e:
                logger.warning(f"firehose dropped: {type(e).__name__}: {e}")
            self.db.commit()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
