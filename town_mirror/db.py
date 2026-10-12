"""Schema and read-only access for the Delvetown mirror."""

import sqlite3

from town_mirror import MIRROR_DB, MIRROR_DIR

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS accounts (
    did          TEXT PRIMARY KEY,
    handle       TEXT,
    display_name TEXT,
    description  TEXT,
    created_at   TEXT,
    active       INTEGER DEFAULT 1,
    rev          TEXT,      -- repo revision at last crawl; unchanged = skip
    crawled_at   TEXT,
    first_seen   TEXT
);
CREATE INDEX IF NOT EXISTS accounts_handle ON accounts(handle);

CREATE TABLE IF NOT EXISTS posts (
    uri          TEXT PRIMARY KEY,
    did          TEXT NOT NULL,
    rkey         TEXT NOT NULL,
    cid          TEXT,
    text         TEXT,
    created_at   TEXT,
    reply_root   TEXT,
    reply_parent TEXT,
    quote_uri    TEXT,
    record       TEXT,      -- the full record JSON, for anything not broken out
    first_seen   TEXT
);
CREATE INDEX IF NOT EXISTS posts_did     ON posts(did, created_at);
CREATE INDEX IF NOT EXISTS posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS posts_root    ON posts(reply_root);
CREATE INDEX IF NOT EXISTS posts_parent  ON posts(reply_parent);
CREATE INDEX IF NOT EXISTS posts_seen    ON posts(first_seen);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    text, content='posts', content_rowid='rowid', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO posts_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- Facets, broken out so "who mentioned X" and "posts tagged Y" are joins.
CREATE TABLE IF NOT EXISTS mentions (uri TEXT, did TEXT, PRIMARY KEY (uri, did));
CREATE INDEX IF NOT EXISTS mentions_did ON mentions(did);
CREATE TABLE IF NOT EXISTS links    (uri TEXT, url TEXT, PRIMARY KEY (uri, url));
CREATE TABLE IF NOT EXISTS tags     (uri TEXT, tag TEXT, PRIMARY KEY (uri, tag));
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS follows (
    uri TEXT PRIMARY KEY, did TEXT, subject TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS follows_subject ON follows(subject);
CREATE INDEX IF NOT EXISTS follows_did     ON follows(did);

CREATE TABLE IF NOT EXISTS likes (
    uri TEXT PRIMARY KEY, did TEXT, subject TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS likes_subject ON likes(subject);

CREATE TABLE IF NOT EXISTS reposts (
    uri TEXT PRIMARY KEY, did TEXT, subject TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS reposts_subject ON reposts(subject);

-- What the author took back: no content, only that it existed.
CREATE TABLE IF NOT EXISTS tombstones (uri TEXT PRIMARY KEY, noticed_at TEXT);

CREATE TABLE IF NOT EXISTS crawl_state (key TEXT PRIMARY KEY, value TEXT);
"""


def connect_rw() -> sqlite3.Connection:
    """The crawler's connection — the only writer."""
    MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MIRROR_DB, timeout=30)
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def connect_ro() -> sqlite3.Connection:
    """For resident tools. Read-only at the SQLite level, so even the raw
    SQL tool cannot write, whatever it is handed."""
    if not MIRROR_DB.exists():
        raise RuntimeError("the Delvetown mirror has not been built yet")
    conn = sqlite3.connect(f"file:{MIRROR_DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
