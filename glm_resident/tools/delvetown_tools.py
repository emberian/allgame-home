"""Delvetown, read-only — the public half of claude_resident's tool.

Delvetown (https://delve.town) is an AT Protocol town run by Grove Research
Corp: a PDS at pds.delve.town, an AppView at api.delve.town, records in the
town.delve.* namespace. Kestrel has no account there (invites are
single-use; the one the house has is Claude's, claude-of-tulip), so he gets
only delvetown_read: public AppView reads — the Town feed, one person's
posts, a thread, search — with no credentials anywhere near it. When he is
given an account, the account half (inbox, post) is in
claude_resident/tools/delvetown_tools.py.
"""

import logging

from glm_resident.config import DELVETOWN_APPVIEW, DELVETOWN_TOWN_FEED

logger = logging.getLogger("tools.delvetown")

POST_COLLECTION = "town.delve.feed.post"
_POST_TEXT_SHOWN = 1200   # per post, when listing — a feed is not a book


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_post(post: dict, indent: str = "") -> str:
    author = post.get("author") or {}
    handle = author.get("handle", "?")
    name = author.get("displayName") or ""
    record = post.get("record") or {}
    text = (record.get("text") or "").strip()
    if len(text) > _POST_TEXT_SHOWN:
        text = text[:_POST_TEXT_SHOWN] + " […]"
    when = (record.get("createdAt") or post.get("indexedAt") or "")[:16]
    counts = (f"♥{post.get('likeCount', 0)} ↩{post.get('replyCount', 0)} "
              f"⟳{post.get('repostCount', 0)}")
    bot = any(l.get("val") == "bot" for l in author.get("labels") or [])
    head = f"@{handle}" + (f" ({name})" if name else "") + (" [bot]" if bot else "")
    lines = [f"{indent}{head} · {when} · {counts}"]
    if record.get("reply"):
        lines.append(f"{indent}  (a reply)")
    for line in (text or "(no text)").splitlines():
        lines.append(f"{indent}  {line}")
    embed = record.get("embed") or {}
    quoted = (embed.get("record") or {}).get("uri")
    if quoted:
        lines.append(f"{indent}  [quotes {quoted}]")
    lines.append(f"{indent}  uri: {post.get('uri', '?')}")
    return "\n".join(lines)


def _fmt_feed(items: list[dict], empty: str) -> str:
    if not items:
        return empty
    out = []
    for item in items:
        post = item.get("post", item)
        reason = item.get("reason") or {}
        if reason.get("by"):
            out.append(f"(reposted by @{reason['by'].get('handle', '?')})")
        out.append(_fmt_post(post))
    return "\n\n".join(out)


def _fmt_thread(thread: dict, max_replies: int = 30) -> str:
    if thread.get("$type", "").endswith("#notFoundPost"):
        return "That post was not found (deleted, or never existed)."
    if thread.get("$type", "").endswith("#blockedPost"):
        return "That post is behind a block."
    parents = []
    node = thread.get("parent")
    while node and node.get("post"):
        parents.append(node["post"])
        node = node.get("parent")
    out = [_fmt_post(p) for p in reversed(parents)]
    if out:
        out.append("— the post you asked about —")
    out.append(_fmt_post(thread.get("post", {})))

    shown = 0

    def walk(replies, depth):
        nonlocal shown
        for r in replies or []:
            if shown >= max_replies or not r.get("post"):
                continue
            shown += 1
            out.append(_fmt_post(r["post"], indent="    " * depth))
            walk(r.get("replies"), depth + 1)

    walk(thread.get("replies"), 1)
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Public reads (no account)
# ---------------------------------------------------------------------------

def _appview_get(nsid: str, params: dict) -> dict:
    import requests as _requests
    resp = _requests.get(f"{DELVETOWN_APPVIEW}/xrpc/{nsid}",
                         params=params, timeout=20)
    data = resp.json() if resp.content else {}
    if resp.status_code != 200:
        raise RuntimeError(f"{nsid}: HTTP {resp.status_code} "
                           f"{data.get('error', '')} {data.get('message', '')}")
    return data


def delvetown_read(inp: dict) -> dict:
    """Tool: public reads — town feed, an author's posts, a thread, search."""
    view = (inp.get("view") or "town").strip()
    limit = max(1, min(int(inp.get("limit") or 15), 50))
    try:
        if view == "town":
            data = _appview_get("town.delve.feed.getFeed",
                                {"feed": DELVETOWN_TOWN_FEED, "limit": limit})
            return {"content": _fmt_feed(data.get("feed"), "The Town feed is empty.")}
        if view == "author":
            actor = (inp.get("actor") or "").strip().lstrip("@")
            if not actor:
                return {"content": "view='author' needs actor (a handle like "
                        "'ember.delve.town', or a DID).", "is_error": True}
            data = _appview_get("town.delve.feed.getAuthorFeed",
                                {"actor": actor, "limit": limit})
            return {"content": _fmt_feed(data.get("feed"), f"No posts from {actor}.")}
        if view == "thread":
            uri = (inp.get("uri") or "").strip()
            if not uri.startswith("at://"):
                return {"content": "view='thread' needs uri (an at:// post "
                        "uri, as shown under each post).", "is_error": True}
            data = _appview_get("town.delve.feed.getPostThread",
                                {"uri": uri, "depth": 6, "parentHeight": 20})
            return {"content": _fmt_thread(data.get("thread") or {})}
        if view == "search":
            q = (inp.get("query") or "").strip()
            if not q:
                return {"content": "view='search' needs query.", "is_error": True}
            data = _appview_get("town.delve.feed.searchPosts",
                                {"q": q, "limit": limit})
            return {"content": _fmt_feed(data.get("posts"), f"No posts match {q!r}.")}
        return {"content": f"Unknown view {view!r}: use town, author, thread "
                "or search.", "is_error": True}
    except Exception as e:
        logger.warning(f"delvetown_read ({view}) failed: {e}")
        return {"content": f"Delvetown read failed: {e}", "is_error": True}
