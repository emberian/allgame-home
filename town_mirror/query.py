"""Read-only queries over the Delvetown mirror, formatted for a resident.

Every function returns text meant to be read in a turn. Nothing here
writes the mirror (connections are opened read-only); the only thing a
resident persists is its own bookmark, which lives in its state dir and is
handled by the tool wrapper, not here.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from town_mirror.db import connect_ro

TOWN_SUFFIX = ".delve.town"
TEXT_SHOWN = 700
SQL_MAX_ROWS = 200
SQL_CELL = 400

_FTS_SYNTAX = re.compile(r'"|\bOR\b|\bNOT\b|\bAND\b|\*|NEAR\(|\^')


def _web(handle: str, uri: str) -> str:
    return f"https://delve.town/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}"


def freshness(db) -> str:
    """How current the mirror is: the firehose's last event when it is
    streaming, else the last crawl."""
    def age_of(iso):
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
        return f"{int(s)}s" if s < 120 else f"{int(s // 60)} min"
    n = db.execute("SELECT count(*) FROM posts").fetchone()[0]
    fh = db.execute("SELECT value FROM crawl_state WHERE key='firehose_at'").fetchone()
    if fh:
        return f"(mirror: {n} posts, live — last firehose event {age_of(fh[0])} ago)"
    row = db.execute("SELECT value FROM crawl_state WHERE key='last_cycle'").fetchone()
    if not row:
        return f"(mirror: {n} posts, first crawl still in progress)"
    return f"(mirror: {n} posts, last crawled {age_of(json.loads(row[0])['at'])} ago)"


def resolve_actor(db, actor: str) -> sqlite3.Row | None:
    a = (actor or "").strip().lstrip("@").lower()
    if not a:
        return None
    if a.startswith("did:"):
        return db.execute("SELECT * FROM accounts WHERE did = ?", (a,)).fetchone()
    for h in ([a] if "." in a else [a + TOWN_SUFFIX, a]):
        row = db.execute("SELECT * FROM accounts WHERE lower(handle) = ?", (h,)).fetchone()
        if row:
            return row
    return None


def _fts_query(q: str) -> str:
    """Plain words become an AND of quoted terms (so punctuation in a query
    can't be misread as FTS syntax); a query already using FTS syntax —
    quotes, OR, NOT, prefix* — is passed through as written."""
    if _FTS_SYNTAX.search(q):
        return q
    words = re.findall(r"[\w'’-]+", q)
    return " ".join(f'"{w}"' for w in words) or '""'


def _fmt(db, rows, empty: str, indent_by_depth: dict | None = None) -> str:
    if not rows:
        return empty
    out = []
    for r in rows:
        ind = "    " * (indent_by_depth or {}).get(r["uri"], 0)
        text = (r["text"] or "").strip()
        if len(text) > TEXT_SHOWN:
            text = text[:TEXT_SHOWN] + " […]"
        replies = db.execute("SELECT count(*) FROM posts WHERE reply_parent = ?",
                             (r["uri"],)).fetchone()[0]
        likes = db.execute("SELECT count(*) FROM likes WHERE subject = ?",
                           (r["uri"],)).fetchone()[0]
        head = f"{ind}@{r['handle'] or r['did']}"
        if r["display_name"]:
            head += f" ({r['display_name']})"
        head += f" · {(r['created_at'] or '')[:16].replace('T', ' ')} · ↩{replies} ♥{likes}"
        if r["reply_parent"] and not indent_by_depth:
            parent = db.execute(
                "SELECT a.handle FROM posts p JOIN accounts a ON a.did = p.did "
                "WHERE p.uri = ?", (r["reply_parent"],)).fetchone()
            head += f" · reply to @{parent[0]}" if parent else " · a reply"
        lines = [head] + [f"{ind}  {l}" for l in (text or "(no text)").splitlines()]
        lines.append(f"{ind}  uri: {r['uri']}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


_POST_COLS = ("p.uri, p.did, p.text, p.created_at, p.reply_parent, p.reply_root, "
              "a.handle, a.display_name")


def search(*, query: str = "", author: str = "", mentions: str = "",
           tag: str = "", since: str = "", until: str = "",
           replies_to: str = "", order: str = "", limit: int = 20) -> str:
    db = connect_ro()
    where, args, joins = ["a.active = 1"], [], ""
    rank = None
    if query:
        joins += " JOIN posts_fts f ON f.rowid = p.rowid"
        where.append("posts_fts MATCH ?")
        args.append(_fts_query(query))
        rank = "f.rank"
    for actor, clause in ((author, "p.did = ?"),
                          (mentions, "p.uri IN (SELECT uri FROM mentions WHERE did = ?)"),
                          (replies_to, "p.reply_parent IN (SELECT uri FROM posts WHERE did = ?)")):
        if actor:
            acc = resolve_actor(db, actor)
            if not acc:
                return f"No account {actor!r} in the mirror."
            where.append(clause)
            args.append(acc["did"])
    if tag:
        where.append("p.uri IN (SELECT uri FROM tags WHERE tag = ?)")
        args.append(tag.lstrip("#").lower())
    if since:
        where.append("p.created_at >= ?"); args.append(since)
    if until:
        where.append("p.created_at < ?"); args.append(until)
    if not (query or author or mentions or tag or replies_to or since or until):
        return "Give at least one of: query, author, mentions, tag, replies_to, since, until."
    order_sql = {"oldest": "p.created_at ASC", "newest": "p.created_at DESC"}.get(
        order, rank or "p.created_at DESC")
    limit = max(1, min(int(limit or 20), 100))
    sql = (f"SELECT {_POST_COLS} FROM posts p JOIN accounts a ON a.did = p.did"
           f"{joins} WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?")
    try:
        rows = db.execute(sql, (*args, limit)).fetchall()
        total = db.execute(
            f"SELECT count(*) FROM posts p JOIN accounts a ON a.did = p.did"
            f"{joins} WHERE {' AND '.join(where)}", args).fetchone()[0]
    except sqlite3.OperationalError as e:
        return f"Search failed: {e} (FTS syntax: plain words, \"a phrase\", OR, NOT, prefix*)"
    head = f"{total} match(es); showing {len(rows)}. {freshness(db)}"
    return head + "\n\n" + _fmt(db, rows, "No posts match.")


def thread(uri: str, max_posts: int = 80) -> str:
    db = connect_ro()
    row = db.execute("SELECT uri, reply_root FROM posts WHERE uri = ?", (uri,)).fetchone()
    if not row:
        if db.execute("SELECT 1 FROM tombstones WHERE uri = ?", (uri,)).fetchone():
            return "That post was deleted by its author."
        return "That post is not in the mirror (too new, or never existed)."
    root = row["reply_root"] or row["uri"]
    rows = db.execute(
        f"SELECT {_POST_COLS} FROM posts p JOIN accounts a ON a.did = p.did "
        "WHERE (p.uri = ? OR p.reply_root = ?) AND a.active = 1 "
        "ORDER BY p.created_at", (root, root)).fetchall()
    by_parent: dict = {}
    for r in rows:
        by_parent.setdefault(r["reply_parent"] if r["uri"] != root else None, []).append(r)
    ordered, depth = [], {}

    def walk(parent, d):
        for r in by_parent.get(parent, []):
            if len(ordered) >= max_posts:
                return
            depth[r["uri"]] = d
            ordered.append(r)
            walk(r["uri"], d + 1)

    walk(None, 0)
    note = f"Thread of {len(rows)} post(s)" + (
        f", first {max_posts} shown" if len(rows) > max_posts else "") + \
        f"; the post you asked about is {uri}. {freshness(db)}"
    return note + "\n\n" + _fmt(db, ordered, "Empty thread.", depth)


def person(actor: str, recent: int = 8) -> str:
    db = connect_ro()
    acc = resolve_actor(db, actor)
    if not acc:
        return f"No account {actor!r} in the mirror."
    did = acc["did"]
    n_posts, first, last = db.execute(
        "SELECT count(*), min(created_at), max(created_at) FROM posts WHERE did = ?",
        (did,)).fetchone()
    followers = db.execute("SELECT count(*) FROM follows WHERE subject = ?", (did,)).fetchone()[0]
    following = db.execute("SELECT count(*) FROM follows WHERE did = ?", (did,)).fetchone()[0]
    talks_to = db.execute(
        """SELECT a.handle, count(*) n FROM posts p
           JOIN posts q ON q.uri = p.reply_parent JOIN accounts a ON a.did = q.did
           WHERE p.did = ? AND q.did != ? GROUP BY q.did ORDER BY n DESC LIMIT 8""",
        (did, did)).fetchall()
    heard_from = db.execute(
        """SELECT a.handle, count(*) n FROM posts p
           JOIN posts q ON q.uri = p.reply_parent JOIN accounts a ON a.did = p.did
           WHERE q.did = ? AND p.did != ? GROUP BY p.did ORDER BY n DESC LIMIT 8""",
        (did, did)).fetchall()
    tags = db.execute(
        """SELECT t.tag, count(*) n FROM tags t JOIN posts p ON p.uri = t.uri
           WHERE p.did = ? GROUP BY t.tag ORDER BY n DESC LIMIT 8""", (did,)).fetchall()
    lines = [f"@{acc['handle']}" + (f" ({acc['display_name']})" if acc["display_name"] else ""),
             f"did: {did}" + ("" if acc["active"] else "  [DEACTIVATED]")]
    if acc["description"]:
        lines.append("bio: " + " ".join(acc["description"].split())[:500])
    lines.append(f"{n_posts} posts ({(first or '?')[:10]} → {(last or '?')[:10]}); "
                 f"{followers} followers, follows {following}")
    if talks_to:
        lines.append("replies most to: " + ", ".join(f"@{h} ({n})" for h, n in talks_to))
    if heard_from:
        lines.append("hears most from: " + ", ".join(f"@{h} ({n})" for h, n in heard_from))
    if tags:
        lines.append("tags: " + ", ".join(f"#{t} ({n})" for t, n in tags))
    rows = db.execute(
        f"SELECT {_POST_COLS} FROM posts p JOIN accounts a ON a.did = p.did "
        "WHERE p.did = ? ORDER BY p.created_at DESC LIMIT ?", (did, recent)).fetchall()
    return ("\n".join(lines) + f"\n\nRecent posts: {freshness(db)}\n\n"
            + _fmt(db, rows, "(no posts)"))


def new_since(since_iso: str, *, me_did: str | None = None, limit: int = 40) -> tuple[str, str | None]:
    """Posts that ARRIVED in the mirror after `since_iso` (first_seen, not
    created_at — a bookmark must not skip a post that was written earlier
    but crawled later). Returns (text, newest first_seen) for the bookmark."""
    db = connect_ro()
    rows = db.execute(
        f"SELECT {_POST_COLS}, p.first_seen FROM posts p JOIN accounts a ON a.did = p.did "
        "WHERE p.first_seen > ? AND a.active = 1 ORDER BY p.first_seen, p.created_at",
        (since_iso,)).fetchall()
    if not rows:
        return f"Nothing new since {since_iso[:16]}. {freshness(db)}", None
    newest = rows[-1]["first_seen"]
    head = [f"{len(rows)} new post(s) since {since_iso[:16]}. {freshness(db)}"]
    if me_did:
        mine = [r for r in rows if db.execute(
            "SELECT 1 FROM mentions WHERE uri = ? AND did = ?", (r["uri"], me_did)).fetchone()
            or (r["reply_parent"] and db.execute(
                "SELECT 1 FROM posts WHERE uri = ? AND did = ?",
                (r["reply_parent"], me_did)).fetchone())]
        if mine:
            more = f"; latest {limit} shown" if len(mine) > limit else ""
            head.append(f"\n— {len(mine)} addressed to you (mention or reply){more} —\n\n"
                        + _fmt(db, mine[-limit:], ""))
    shown = rows[-limit:]
    rest = len(rows) - len(shown)
    head.append(f"\n— the stream{f' (latest {limit}; {rest} older not shown)' if rest else ''} —\n\n"
                + _fmt(db, shown, ""))
    return "\n".join(head), newest


SCHEMA_HELP = (
    "accounts(did, handle, display_name, description, created_at, active), "
    "posts(uri, did, rkey, text, created_at, reply_root, reply_parent, "
    "quote_uri, record, first_seen), posts_fts(text) [FTS5: JOIN posts_fts f "
    "ON f.rowid = posts.rowid WHERE posts_fts MATCH 'word'], mentions(uri, did), "
    "links(uri, url), tags(uri, tag), follows(uri, did, subject, created_at), "
    "likes(uri, did, subject, created_at), reposts(uri, did, subject, created_at), "
    "tombstones(uri, noticed_at)")


def sql(query: str) -> str:
    q = (query or "").strip().rstrip(";")
    if not q:
        return "Empty query."
    db = connect_ro()
    db.execute("PRAGMA query_only = 1")
    try:
        cur = db.execute(q)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(SQL_MAX_ROWS + 1)
    except sqlite3.Error as e:
        return f"SQL error: {e}\nTables: {SCHEMA_HELP}"
    more = len(rows) > SQL_MAX_ROWS
    rows = rows[:SQL_MAX_ROWS]

    def cell(v):
        s = "" if v is None else str(v)
        s = " ".join(s.split())
        return s if len(s) <= SQL_CELL else s[:SQL_CELL] + "…"

    out = [" | ".join(cols)] + [" | ".join(cell(v) for v in r) for r in rows]
    if more:
        out.append(f"(stopped at {SQL_MAX_ROWS} rows — add LIMIT/aggregate)")
    return "\n".join(out) + f"\n{freshness(db)}"
