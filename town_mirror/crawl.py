"""The Delvetown crawler. One process for the whole house.

    python -m town_mirror           firehose live + hourly reconcile crawl
    python -m town_mirror --once    one cycle, then exit

A cycle lists every repo on the PDS (com.atproto.sync.listRepos, which
carries each repo's current `rev`), and re-crawls only the repos whose rev
moved since last time. A re-crawl lists each tracked collection IN FULL
rather than paging until a known record: that is how deletions are seen.
At town scale (hundreds of accounts, tens of thousands of records) a full
listing of the handful of repos that changed in five minutes is cheap.

Politeness: sequential, one request at a time, a short pause between
requests, and nothing re-fetched that has not changed.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone

import requests

from town_mirror import NS, PDS
from town_mirror.db import connect_rw

logger = logging.getLogger("town_mirror")

PAUSE_S = 0.05
PAGE = 100

COLLECTIONS = {
    f"{NS}.feed.post": "posts",
    f"{NS}.graph.follow": "follows",
    f"{NS}.feed.like": "likes",
    f"{NS}.feed.repost": "reposts",
}
PROFILE = f"{NS}.actor.profile"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Applying records — shared by the crawler (backfill, reconcile) and the
# firehose (live). Both are idempotent: the same record applied twice, or by
# both paths in either order, leaves one row.
# ---------------------------------------------------------------------------

def apply_record(db, did: str, collection: str, uri: str, cid, value: dict,
                 now: str):
    table = COLLECTIONS.get(collection)
    if table is None:
        if collection == PROFILE:
            db.execute("UPDATE accounts SET display_name = ?, description = ? "
                       "WHERE did = ?",
                       (value.get("displayName"), value.get("description"), did))
        return
    if table != "posts":
        subj = value.get("subject")
        if isinstance(subj, dict):
            subj = subj.get("uri")
        db.execute(f"INSERT OR REPLACE INTO {table} "
                   "(uri, did, subject, created_at) VALUES (?,?,?,?)",
                   (uri, did, subj, value.get("createdAt")))
        return
    prev = db.execute("SELECT cid FROM posts WHERE uri = ?", (uri,)).fetchone()
    if prev and cid and prev[0] == cid:
        return
    reply = value.get("reply") or {}
    embed = value.get("embed") or {}
    quote = ((embed.get("record") or {}).get("uri")
             or ((embed.get("record") or {}).get("record") or {}).get("uri"))
    db.execute(
        """INSERT INTO posts (uri, did, rkey, cid, text, created_at,
               reply_root, reply_parent, quote_uri, record, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(uri) DO UPDATE SET
               cid=excluded.cid, text=excluded.text,
               reply_root=excluded.reply_root,
               reply_parent=excluded.reply_parent,
               quote_uri=excluded.quote_uri, record=excluded.record""",
        (uri, did, uri.rsplit("/", 1)[-1], cid, value.get("text") or "",
         value.get("createdAt"), (reply.get("root") or {}).get("uri"),
         (reply.get("parent") or {}).get("uri"), quote,
         json.dumps(value, ensure_ascii=False, default=str), now))
    for t in ("mentions", "links", "tags"):
        db.execute(f"DELETE FROM {t} WHERE uri = ?", (uri,))
    for f in value.get("facets") or []:
        for feat in f.get("features") or []:
            kind = (feat.get("$type") or "").rsplit("#", 1)[-1]
            if kind == "mention" and feat.get("did"):
                db.execute("INSERT OR IGNORE INTO mentions VALUES (?,?)",
                           (uri, feat["did"]))
            elif kind == "link" and feat.get("uri"):
                db.execute("INSERT OR IGNORE INTO links VALUES (?,?)",
                           (uri, feat["uri"]))
            elif kind == "tag" and feat.get("tag"):
                db.execute("INSERT OR IGNORE INTO tags VALUES (?,?)",
                           (uri, feat["tag"].lower()))


def delete_record(db, collection: str, uri: str, now: str):
    table = COLLECTIONS.get(collection)
    if table is None:
        return
    db.execute(f"DELETE FROM {table} WHERE uri = ?", (uri,))
    if table == "posts":
        for t in ("mentions", "links", "tags"):
            db.execute(f"DELETE FROM {t} WHERE uri = ?", (uri,))
        db.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?)", (uri, now))


def forget_account(db, did: str):
    """An account gone from the PDS has taken everything back."""
    now = _now()
    uris = [r[0] for r in db.execute("SELECT uri FROM posts WHERE did = ?", (did,))]
    for u in uris:
        db.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?)", (u, now))
        for t in ("mentions", "links", "tags"):
            db.execute(f"DELETE FROM {t} WHERE uri = ?", (u,))
    for t in ("posts", "follows", "likes", "reposts"):
        db.execute(f"DELETE FROM {t} WHERE did = ?", (did,))
    db.execute("DELETE FROM accounts WHERE did = ?", (did,))
    logger.info(f"account {did} left the PDS: {len(uris)} posts forgotten")


class Crawler:
    def __init__(self):
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "allgame-town-mirror (ember.software)"
        self.db = connect_rw()
        self.requests = 0

    def _get(self, nsid: str, **params) -> dict | None:
        time.sleep(PAUSE_S)
        self.requests += 1
        for attempt in range(3):
            try:
                r = self.http.get(f"{PDS}/xrpc/{nsid}", params=params, timeout=30)
            except requests.RequestException as e:
                logger.warning(f"{nsid}: {e} (attempt {attempt + 1})")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            # 400 RepoNotFound / RepoDeactivated etc: a fact, not a fault.
            return None
        return None

    # ---- enumeration ----------------------------------------------------

    def list_repos(self) -> list[dict]:
        repos, cursor = [], None
        while True:
            p = {"limit": 1000}
            if cursor:
                p["cursor"] = cursor
            d = self._get("com.atproto.sync.listRepos", **p)
            if d is None:
                raise RuntimeError("listRepos failed")
            repos += d.get("repos", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("repos"):
                return repos

    def list_records(self, did: str, collection: str) -> list[dict] | None:
        out, cursor = [], None
        while True:
            p = {"repo": did, "collection": collection, "limit": PAGE}
            if cursor:
                p["cursor"] = cursor
            d = self._get("com.atproto.repo.listRecords", **p)
            if d is None:
                return None if not out else out
            out += d.get("records", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("records"):
                return out

    # ---- one repo -------------------------------------------------------

    def crawl_repo(self, did: str, rev: str) -> dict:
        """Fetch everything first, then write in one short transaction: the
        firehose writes alongside, and holding SQLite's write lock across
        a few hundred HTTP pages for a busy account would stall it."""
        stats = {"posts+": 0, "posts-": 0}
        desc = self._get("com.atproto.repo.describeRepo", repo=did) or {}
        collections = set(desc.get("collections") or [])
        prof = {}
        if PROFILE in collections:
            recs = self.list_records(did, PROFILE) or []
            prof = (recs[0].get("value") if recs else {}) or {}
        fetched, complete = {}, True
        for collection in COLLECTIONS:
            recs = (self.list_records(did, collection)
                    if collection in collections else [])
            if recs is None:
                complete = False    # transient: keep what we have for it
                continue
            fetched[collection] = recs

        db, now = self.db, _now()
        db.execute(
            """INSERT INTO accounts (did, handle, display_name, description,
                   created_at, active, rev, crawled_at, first_seen)
               VALUES (?,?,?,?,?,1,?,?,?)
               ON CONFLICT(did) DO UPDATE SET
                   handle=COALESCE(excluded.handle, accounts.handle),
                   display_name=excluded.display_name,
                   description=excluded.description,
                   created_at=COALESCE(accounts.created_at, excluded.created_at),
                   active=1, crawled_at=excluded.crawled_at""",
            (did, desc.get("handle"), prof.get("displayName"),
             prof.get("description"), prof.get("createdAt"), None, now, now))
        for collection, recs in fetched.items():
            table = COLLECTIONS[collection]
            seen = {r["uri"] for r in recs}
            have = {row[0] for row in db.execute(
                f"SELECT uri FROM {table} WHERE did = ?", (did,))}
            for r in recs:
                if r["uri"] not in have and table == "posts":
                    stats["posts+"] += 1
                apply_record(db, did, collection, r["uri"], r.get("cid"),
                             r.get("value") or {}, now)
            for gone in have - seen:
                delete_record(db, collection, gone, now)
                if table == "posts":
                    stats["posts-"] += 1
        # Only a complete crawl earns the rev: a partial one must be retried.
        if complete:
            db.execute("UPDATE accounts SET rev = ? WHERE did = ?", (rev, did))
        db.commit()
        return stats

    # ---- a cycle --------------------------------------------------------

    def cycle(self) -> dict:
        t0, self.requests = time.time(), 0
        repos = self.list_repos()
        known = {row["did"]: row["rev"] for row in
                 self.db.execute("SELECT did, rev FROM accounts")}
        listed = set()
        changed, totals = 0, {"posts+": 0, "posts-": 0}
        for repo in repos:
            did = repo["did"]
            listed.add(did)
            if not repo.get("active", True):
                self.db.execute("UPDATE accounts SET active = 0 WHERE did = ?", (did,))
                continue
            if known.get(did) == repo.get("rev"):
                continue
            changed += 1
            try:
                s = self.crawl_repo(did, repo.get("rev"))
                for k in totals:
                    totals[k] += s[k]
            except Exception as e:
                logger.warning(f"crawl {did}: {e}")
        # An account gone from the PDS entirely has taken everything back.
        for did in set(known) - listed:
            forget_account(self.db, did)
        stats = {"at": _now(), "repos": len(repos), "changed": changed,
                 "requests": self.requests, "seconds": round(time.time() - t0, 1),
                 **totals,
                 "posts_total": self.db.execute("SELECT count(*) FROM posts").fetchone()[0]}
        self.db.execute("INSERT OR REPLACE INTO crawl_state VALUES ('last_cycle', ?)",
                        (json.dumps(stats),))
        self.db.commit()
        logger.info(f"cycle: {stats}")
        return stats



RECONCILE_S = 3600


def main(argv=None):
    """`--once`: one crawl cycle and exit. Otherwise the daemon: the
    firehose live in the foreground, and a reconcile crawl in a thread —
    immediately if the town has never been crawled, then every hour."""
    import asyncio
    import threading

    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if "--once" in argv:
        Crawler().cycle()
        return

    def reconcile():
        c = Crawler()
        while True:
            try:
                c.cycle()
            except Exception as e:
                logger.error(f"reconcile failed: {e}")
            time.sleep(RECONCILE_S)

    logger.info("town mirror starting: firehose live, reconcile hourly")
    threading.Thread(target=reconcile, name="reconcile", daemon=True).start()
    from town_mirror.stream import Stream
    asyncio.run(Stream().run())
