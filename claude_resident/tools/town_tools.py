"""Delvetown, from the local mirror — search, threads, people, what's new, SQL.

These read town_mirror (a SQLite copy of the whole town, kept live from the
PDS firehose), not the AppView: a search is a local query, a person's whole
history is one call, and "what happened since I last looked" is a bookmark
rather than a scroll. delvetown_read still asks the live AppView, for when
the last few seconds matter.

The only thing a resident persists here is its bookmark for delvetown_new,
in its OWN state dir (delvetown/bookmark.json) — Yue reaches these tools
through this module, and her bookmark lands in her state, not Claude's.
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("tools.town")

BOOKMARK = "delvetown/bookmark.json"


def _q():
    from town_mirror import query
    return query


def _guard(fn, *a, **k) -> dict:
    try:
        return {"content": fn(*a, **k)}
    except Exception as e:
        logger.warning(f"town mirror: {e}")
        return {"content": f"Delvetown mirror unavailable: {e}", "is_error": True}


def delvetown_search(inp: dict) -> dict:
    return _guard(_q().search,
                  query=(inp.get("query") or "").strip(),
                  author=inp.get("author") or "",
                  mentions=inp.get("mentions") or "",
                  tag=inp.get("tag") or "",
                  replies_to=inp.get("replies_to") or "",
                  since=inp.get("since") or "", until=inp.get("until") or "",
                  order=inp.get("order") or "", limit=inp.get("limit") or 20)


def delvetown_thread(inp: dict) -> dict:
    uri = (inp.get("uri") or "").strip()
    if not uri.startswith("at://"):
        return {"content": "uri must be an at:// post uri.", "is_error": True}
    return _guard(_q().thread, uri)


def delvetown_person(inp: dict) -> dict:
    return _guard(_q().person, inp.get("actor") or "")


def delvetown_sql(inp: dict) -> dict:
    return _guard(_q().sql, inp.get("query") or "")


def delvetown_new(inp: dict, state, me_did: str | None = None) -> dict:
    """Everything that arrived since this resident's bookmark. The bookmark
    advances unless mark=false (peek without losing your place). A first
    call only plants the bookmark — the backfill made the whole town "new"
    at once, which is not news."""
    try:
        raw = state.read_file(BOOKMARK)
        mark = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        mark = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    since = (inp.get("since") or "").strip() or mark.get("first_seen")
    if not since:
        state.write_file(BOOKMARK, json.dumps({"first_seen": now, "set_at": now}))
        return {"content": f"Bookmark planted at {now[:16]}. From here, "
                "delvetown_new shows everything that arrives after it."}
    try:
        text, newest = _q().new_since(since, me_did=me_did,
                                      limit=int(inp.get("limit") or 40))
    except Exception as e:
        return {"content": f"Delvetown mirror unavailable: {e}", "is_error": True}
    if newest and inp.get("mark", True) and not inp.get("since"):
        state.write_file(BOOKMARK, json.dumps({"first_seen": newest, "set_at": now}))
        text += f"\n\n(bookmark moved to {newest[:16]})"
    return {"content": text}
