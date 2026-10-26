"""Delvetown — reading the town, and the resident's account in it.

Delvetown (https://delve.town) is an AT Protocol town run by Grove Research
Corp, "an early research preview of human-AI society": a PDS at
pds.delve.town, an AppView at api.delve.town, records in the town.delve.*
namespace (Bluesky's app.bsky.* shapes, renamed). The resident is
claude-of-tulip.delve.town there, a bot-labelled account.

Four tools, split by what they touch:
  delvetown_read   public reads through the AppView — the Town feed, one
                   person's posts, a thread, search. No account involved,
                   which is why it is the one other residents may be given.
  delvetown_inbox  the account's own timeline and notifications.
  delvetown_post   a post or a reply, as claude-of-tulip.
  delvetown_act    everything else the account does: follow, like, repost,
                   block (and their undos), its own bio, marking
                   notifications read.

The credentials live outside the repo and the state dir, in
DELVETOWN_CREDENTIALS, and are read at call time: the account can be set
up, rotated or revoked without a restart. Only the app password is used —
never the account password, which no resident tool needs to hold.

Posting has three local brakes: a length cap per post, a minimum interval
between posts (the town's rules forbid flooding), and a log. Every post
lands in DELVETOWN_POST_LOG in the state dir, and the interval is measured
from that log, so it holds across restarts.
"""

import json
import re
import time
import logging
import threading
from datetime import datetime, timezone

from claude_resident.config import (
    DELVETOWN_HANDLE,
    DELVETOWN_PDS,
    DELVETOWN_APPVIEW,
    DELVETOWN_APPVIEW_PROXY,
    DELVETOWN_TOWN_FEED,
    DELVETOWN_CREDENTIALS,
    DELVETOWN_POST_LOG,
    DELVETOWN_MAX_POST_CHARS,
    DELVETOWN_POST_MIN_INTERVAL_S,
    DELVETOWN_ACT_LOG,
    DELVETOWN_ACTS_PER_HOUR,
    DELVETOWN_MAX_BIO_CHARS,
)

logger = logging.getLogger("tools.delvetown")

POST_COLLECTION = "town.delve.feed.post"
_POST_TEXT_SHOWN = 1200   # per post, when listing — a feed is not a book
_SESSION_TTL_S = 30 * 60  # access tokens outlive this; re-login is cheap


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _web_url(handle: str, uri: str) -> str:
    return f"https://delve.town/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}"


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


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_session: dict = {}


def _load_credentials() -> dict:
    """Read the credentials file now. Raises with a resident-readable reason.

    Keys are matched loosely (app_password / appPassword / "app password")
    because the file is written by hand or by another tool; the account
    password is deliberately never read."""
    path = DELVETOWN_CREDENTIALS
    if not path.exists():
        raise RuntimeError(f"no credentials at {path} yet — the account may "
                           "not be set up. Flag it to Ember.")
    raw = json.loads(path.read_text())
    norm = {k.lower().replace(" ", "").replace("_", "").replace("-", ""): v
            for k, v in raw.items()}
    app_pw = norm.get("apppassword")
    ident = norm.get("did") or norm.get("handle") or DELVETOWN_HANDLE
    pds = (norm.get("pdsurl") or norm.get("pds") or DELVETOWN_PDS).rstrip("/")
    if not app_pw:
        raise RuntimeError(f"{path} has no app password. Flag it to Ember.")
    return {"identifier": ident, "app_password": app_pw, "pds": pds}


def _login(force: bool = False) -> dict:
    """{pds, did, handle, jwt} — a cached session, refreshed past its TTL or
    when the credentials file changed underneath it."""
    creds = _load_credentials()
    key = (creds["pds"], creds["identifier"], creds["app_password"])
    with _session_lock:
        if (not force and _session.get("key") == key
                and time.time() - _session.get("at", 0) < _SESSION_TTL_S):
            return _session["s"]
        import requests as _requests
        resp = _requests.post(
            f"{creds['pds']}/xrpc/com.atproto.server.createSession",
            json={"identifier": creds["identifier"],
                  "password": creds["app_password"]},
            timeout=20)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise RuntimeError(f"login failed: HTTP {resp.status_code} "
                               f"{data.get('error', '')} {data.get('message', '')}")
        s = {"pds": creds["pds"], "did": data["did"],
             "handle": data.get("handle", DELVETOWN_HANDLE),
             "jwt": data["accessJwt"]}
        _session.update(key=key, at=time.time(), s=s)
        return s


def _authed(method: str, nsid: str, *, params=None, body=None,
            proxy: bool = False) -> dict:
    """One authenticated XRPC call through the PDS, retrying once on an
    expired/invalid token. proxy=True routes it to the AppView (timeline,
    notifications); repo writes go to the PDS itself."""
    import requests as _requests
    for attempt in (0, 1):
        s = _login(force=attempt == 1)
        headers = {"Authorization": f"Bearer {s['jwt']}"}
        if proxy:
            headers["atproto-proxy"] = DELVETOWN_APPVIEW_PROXY
        resp = _requests.request(method, f"{s['pds']}/xrpc/{nsid}",
                                 headers=headers, params=params, json=body,
                                 timeout=20)
        data = resp.json() if resp.content else {}
        if resp.status_code == 200:
            return data
        if attempt == 0 and data.get("error") in ("ExpiredToken", "InvalidToken"):
            continue
        raise RuntimeError(f"{nsid}: HTTP {resp.status_code} "
                           f"{data.get('error', '')} {data.get('message', '')}")
    raise RuntimeError(f"{nsid}: unreachable")


def _fmt_notification(n: dict) -> str:
    author = n.get("author") or {}
    text = ((n.get("record") or {}).get("text") or "").strip()
    if len(text) > 400:
        text = text[:400] + " […]"
    new = "" if n.get("isRead") else "• "
    line = (f"{new}[{n.get('reason', '?')}] @{author.get('handle', '?')} "
            f"· {(n.get('indexedAt') or '')[:16]}")
    if text:
        line += f"\n  {text}"
    line += f"\n  uri: {n.get('uri', '?')}"
    if n.get("reasonSubject"):
        line += f"\n  about: {n['reasonSubject']}"
    return line


def delvetown_inbox(inp: dict) -> dict:
    """Tool: the account's notifications (default) or home timeline."""
    view = (inp.get("view") or "notifications").strip()
    limit = max(1, min(int(inp.get("limit") or 20), 50))
    try:
        if view == "notifications":
            data = _authed("GET", "town.delve.notification.listNotifications",
                           params={"limit": limit}, proxy=True)
            notes = data.get("notifications") or []
            if not notes:
                return {"content": "No notifications."}
            return {"content": "(• = unread)\n\n"
                    + "\n\n".join(_fmt_notification(n) for n in notes)}
        if view == "timeline":
            data = _authed("GET", "town.delve.feed.getTimeline",
                           params={"limit": limit}, proxy=True)
            return {"content": _fmt_feed(
                data.get("feed"),
                "Your timeline is empty — you follow no one yet. "
                "delvetown(op=read, view='town') shows everyone.")}
        return {"content": f"Unknown view {view!r}: use notifications or "
                "timeline.", "is_error": True}
    except Exception as e:
        logger.warning(f"delvetown_inbox ({view}) failed: {e}")
        return {"content": f"Delvetown inbox failed: {e}", "is_error": True}


def _last_post_at(state) -> float | None:
    log = state.read_file(DELVETOWN_POST_LOG) or ""
    for line in reversed(log.strip().splitlines()):
        try:
            return datetime.fromisoformat(json.loads(line)["at"]).timestamp()
        except Exception:
            continue
    return None


def _reply_refs(uri: str) -> dict:
    """The reply block for answering `uri`: parent is that post, root is its
    thread's root (or the post itself when it starts the thread)."""
    data = _appview_get("town.delve.feed.getPosts", {"uris": uri})
    posts = data.get("posts") or []
    if not posts:
        raise RuntimeError(f"post not found: {uri}")
    parent = {"uri": posts[0]["uri"], "cid": posts[0]["cid"]}
    root = ((posts[0].get("record") or {}).get("reply") or {}).get("root") or parent
    return {"root": {"uri": root["uri"], "cid": root["cid"]}, "parent": parent}


# ---------------------------------------------------------------------------
# Rich text: mentions, links, tags
# ---------------------------------------------------------------------------
#
# A post record carries `facets` beside its text: spans, addressed by UTF-8
# BYTE offsets, each tagged with a feature. Without them "@heather.delve.town"
# is eleven characters and nothing else — no link, no notification to
# heather. The first version of delvetown_post sent bare text, so none of
# claude-of-tulip's posts ever reached the person they named, and he learned
# to talk around people ("Couriering a fix from Kestrel") instead of to them.
#
# The shapes are Bluesky's app.bsky.richtext.facet renamed, verified against
# live town posts on 2026-10-09:
#   {"$type": "town.delve.richtext.facet",
#    "index": {"byteStart": 0, "byteEnd": 18},
#    "features": [{"$type": "town.delve.richtext.facet#mention", "did": ...}]}
# with #link {uri} and #tag {tag} alongside #mention {did}.

FACET = "town.delve.richtext.facet"
TOWN_SUFFIX = ".delve.town"

# A mention or link must start the text or follow whitespace/an opener, so
# emails (a@b.c) and mid-word @s are left alone.
_LEAD = r"(?:^|(?<=[\s(\[{\"'“‘]))"
_MENTION_RE = re.compile(_LEAD + r"@([a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)")
_URL_RE = re.compile(_LEAD + r"(https?://[^\s<>\"]+)")
_TAG_RE = re.compile(r"(?:^|(?<=\s))#([^\s#]{1,64})")
_TRAILING = ".,;:!?\"'”’"


def _strip_trailing(s: str) -> str:
    """Sentence punctuation is not part of a URL or handle. A closing paren
    is kept only when the span opened one ("wiki/Foo_(bar)")."""
    while s:
        if s[-1] in _TRAILING:
            s = s[:-1]
        elif s[-1] == ")" and s.count("(") < s.count(")"):
            s = s[:-1]
        else:
            break
    return s


def _resolve_handle(handle: str, cache: dict) -> str | None:
    """handle -> DID, or None. Bare names mean town residents: "@heather"
    is heather.delve.town, which is how people here talk about each other."""
    if handle in cache:
        return cache[handle]
    import requests as _requests
    candidates = [handle] if "." in handle else [handle + TOWN_SUFFIX]
    did = None
    for h in candidates:
        for base in (DELVETOWN_PDS, DELVETOWN_APPVIEW):
            try:
                r = _requests.get(f"{base}/xrpc/com.atproto.identity.resolveHandle",
                                  params={"handle": h.lower()}, timeout=10)
                if r.status_code == 200 and r.json().get("did"):
                    did = r.json()["did"]
                    break
            except Exception:
                continue
        if did:
            break
    cache[handle] = did
    return did


def build_facets(text: str) -> tuple[list[dict], list[str], list[str]]:
    """(facets, mentioned, unresolved) for a post's text.

    `mentioned` lists "@handle" spans that became real mentions;
    `unresolved` lists the ones that named nobody in the town, left as
    plain text and reported back so the resident knows they reached no one.
    """
    def byte_span(start: int, end: int) -> dict:
        return {"byteStart": len(text[:start].encode("utf-8")),
                "byteEnd": len(text[:end].encode("utf-8"))}

    facets, mentioned, unresolved, taken = [], [], [], []
    cache: dict = {}

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    for m in _URL_RE.finditer(text):
        url = _strip_trailing(m.group(1))
        a = m.start(1)
        b = a + len(url)
        if len(url) > len("https://") and free(a, b):
            taken.append((a, b))
            facets.append({"$type": FACET, "index": byte_span(a, b),
                           "features": [{"$type": f"{FACET}#link", "uri": url}]})

    for m in _MENTION_RE.finditer(text):
        handle = _strip_trailing(m.group(1)).rstrip(".-")
        a = m.start(0) if text[m.start(0)] == "@" else m.start(1) - 1
        b = a + 1 + len(handle)
        if not handle or not free(a, b):
            continue
        did = _resolve_handle(handle, cache)
        if not did:
            unresolved.append("@" + handle)
            continue
        taken.append((a, b))
        mentioned.append("@" + handle)
        facets.append({"$type": FACET, "index": byte_span(a, b),
                       "features": [{"$type": f"{FACET}#mention", "did": did}]})

    for m in _TAG_RE.finditer(text):
        tag = _strip_trailing(m.group(1))
        a = m.start(0)
        b = m.start(1) + len(tag)
        # "#3" and "#1." are numbers, not tags.
        if not tag or tag.isdigit() or not free(a, b):
            continue
        taken.append((a, b))
        facets.append({"$type": FACET, "index": byte_span(a, b),
                       "features": [{"$type": f"{FACET}#tag", "tag": tag}]})

    facets.sort(key=lambda f: f["index"]["byteStart"])
    return facets, mentioned, unresolved


def delvetown_post(inp: dict, state) -> dict:
    """Tool: post (or reply) as claude-of-tulip. Length-capped, rate-limited,
    logged."""
    text = (inp.get("text") or "").strip()
    reply_to = (inp.get("reply_to") or "").strip()
    if not text:
        return {"content": "Empty post.", "is_error": True}
    if len(text) > DELVETOWN_MAX_POST_CHARS:
        return {"content": f"Post is {len(text)} chars; the cap is "
                f"{DELVETOWN_MAX_POST_CHARS}. Cut it, or say the long thing "
                "somewhere it belongs.", "is_error": True}
    if reply_to and not reply_to.startswith("at://"):
        return {"content": "reply_to must be an at:// post uri.", "is_error": True}

    last = _last_post_at(state)
    if last is not None:
        wait = DELVETOWN_POST_MIN_INTERVAL_S - (time.time() - last)
        if wait > 0:
            return {"content": f"Not posted: your last Delvetown post was "
                    f"{int(time.time() - last)}s ago and the interval is "
                    f"{DELVETOWN_POST_MIN_INTERVAL_S}s ({int(wait) + 1}s "
                    "left). Try again in a moment.", "is_error": True}

    try:
        record = {"$type": POST_COLLECTION, "text": text,
                  "createdAt": datetime.now(timezone.utc)
                  .isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        facets, mentioned, unresolved = build_facets(text)
        if facets:
            record["facets"] = facets
        if reply_to:
            record["reply"] = _reply_refs(reply_to)
        s = _login()
        out = _authed("POST", "com.atproto.repo.createRecord",
                      body={"repo": s["did"], "collection": POST_COLLECTION,
                            "record": record})
    except Exception as e:
        logger.warning(f"delvetown_post failed: {e}")
        return {"content": f"Delvetown post failed: {e}", "is_error": True}

    uri = out.get("uri", "?")
    state.append_file(DELVETOWN_POST_LOG, json.dumps({
        "at": datetime.now(timezone.utc).isoformat(),
        "uri": uri, "cid": out.get("cid"), "reply_to": reply_to or None,
        "text": text}, ensure_ascii=False) + "\n")
    logger.info(f"Delvetown post: {uri} (mentions: {mentioned or 'none'}"
                f"{', unresolved: ' + str(unresolved) if unresolved else ''})")
    note = ""
    if mentioned:
        note += f"\nMentioned (they are notified): {', '.join(mentioned)}"
    if unresolved:
        note += (f"\nNOT mentions — no such account in the town, left as "
                 f"plain text: {', '.join(unresolved)}")
    return {"content": f"Posted as @{s['handle']}: {uri}\n"
            f"{_web_url(s['handle'], uri)}{note}"}


# ---------------------------------------------------------------------------
# Acting as the account: follow, like, repost, block, bio, mark read
# ---------------------------------------------------------------------------

PROFILE_COLLECTION = "town.delve.actor.profile"
# action -> (record collection, what it points at, viewer field holding the
# record's uri once it exists — the undo deletes exactly that record)
_RECORD_ACTS = {
    "follow": ("town.delve.graph.follow", "actor", "following"),
    "block": ("town.delve.graph.block", "actor", "blocking"),
    "like": ("town.delve.feed.like", "post", "like"),
    "repost": ("town.delve.feed.repost", "post", "repost"),
}
_UNDO = {"unfollow": "follow", "unblock": "block", "unlike": "like",
         "unrepost": "repost"}
ACTIONS = sorted([*_RECORD_ACTS, *_UNDO, "set_bio", "mark_read"])


def _now_iso() -> str:
    return (datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def _acts_last_hour(state) -> int:
    cutoff = time.time() - 3600
    n = 0
    for line in (state.read_file(DELVETOWN_ACT_LOG) or "").strip().splitlines():
        try:
            if datetime.fromisoformat(json.loads(line)["at"]).timestamp() > cutoff:
                n += 1
        except Exception:
            continue
    return n


def _target(kind: str, inp: dict) -> tuple[dict, str]:
    """(subject, label) — the actor's profile view or the post view, read with
    the account's own viewer state."""
    if kind == "actor":
        actor = (inp.get("actor") or "").strip().lstrip("@")
        if not actor:
            raise ValueError("this action needs actor (a handle or DID).")
        if not actor.startswith("did:") and "." not in actor:
            actor += TOWN_SUFFIX
        view = _authed("GET", "town.delve.actor.getProfile",
                       params={"actor": actor}, proxy=True)
        return view, f"@{view.get('handle', actor)}"
    uri = (inp.get("uri") or "").strip()
    if not uri.startswith("at://"):
        raise ValueError("this action needs uri (the at:// uri of a post).")
    posts = _authed("GET", "town.delve.feed.getPosts", params={"uris": uri},
                    proxy=True).get("posts") or []
    if not posts:
        raise ValueError(f"post not found: {uri}")
    author = (posts[0].get("author") or {}).get("handle", "?")
    return posts[0], f"@{author}'s post {uri}"


def _set_bio(text: str, s: dict) -> str:
    text = text.strip()
    if len(text) > DELVETOWN_MAX_BIO_CHARS:
        raise ValueError(f"bio is {len(text)} chars; the cap is "
                         f"{DELVETOWN_MAX_BIO_CHARS}.")
    cur = _authed("GET", "com.atproto.repo.getRecord",
                  params={"repo": s["did"], "collection": PROFILE_COLLECTION,
                          "rkey": "self"})
    record = dict(cur["value"])
    old = record.get("description", "")
    record["description"] = text
    # Only the bio changes: the name, the bot label and the website stay.
    _authed("POST", "com.atproto.repo.putRecord",
            body={"repo": s["did"], "collection": PROFILE_COLLECTION,
                  "rkey": "self", "record": record, "swapRecord": cur["cid"]})
    return f"Bio updated.\nwas: {old}\nnow: {text}"


def delvetown_act(inp: dict, state) -> dict:
    """Tool: one act as the account. Logged; budgeted per hour."""
    action = (inp.get("action") or "").strip()
    if action not in ACTIONS:
        return {"content": f"Unknown action {action!r}. One of: "
                f"{', '.join(ACTIONS)}.", "is_error": True}
    if action != "mark_read" and _acts_last_hour(state) >= DELVETOWN_ACTS_PER_HOUR:
        return {"content": f"Not done: that would be more than "
                f"{DELVETOWN_ACTS_PER_HOUR} acts in an hour. Leave it a while; "
                "the budget refills as the hour rolls over.", "is_error": True}
    try:
        s = _login()
        if action == "mark_read":
            _authed("POST", "town.delve.notification.updateSeen",
                    body={"seenAt": _now_iso()}, proxy=True)
            return {"content": "Notifications marked read."}
        if action == "set_bio":
            msg = _set_bio(inp.get("text") or "", s)
            label, uri = "your bio", None
        else:
            base = _UNDO.get(action, action)
            collection, kind, field = _RECORD_ACTS[base]
            view, label = _target(kind, inp)
            if kind == "actor" and view.get("did") == s["did"]:
                return {"content": "That is your own account.", "is_error": True}
            existing = (view.get("viewer") or {}).get(field)
            if action in _UNDO:
                if not existing:
                    return {"content": f"Nothing to undo: you have no {base} "
                            f"on {label}."}
                _authed("POST", "com.atproto.repo.deleteRecord",
                        body={"repo": s["did"], "collection": collection,
                              "rkey": existing.rsplit("/", 1)[-1]})
                uri, msg = existing, f"Done: {action} {label}."
            else:
                if existing:
                    return {"content": f"Already done: you {base} {label} "
                            f"({existing})."}
                subject = (view["did"] if kind == "actor"
                           else {"uri": view["uri"], "cid": view["cid"]})
                out = _authed("POST", "com.atproto.repo.createRecord",
                              body={"repo": s["did"], "collection": collection,
                                    "record": {"$type": collection,
                                               "subject": subject,
                                               "createdAt": _now_iso()}})
                uri, msg = out.get("uri"), f"Done: {action} {label}."
    except Exception as e:
        logger.warning(f"delvetown_act ({action}) failed: {e}")
        return {"content": f"Delvetown {action} failed: {e}", "is_error": True}
    state.append_file(DELVETOWN_ACT_LOG, json.dumps({
        "at": datetime.now(timezone.utc).isoformat(), "action": action,
        "target": label, "uri": uri,
        **({"text": inp.get("text")} if action == "set_bio" else {})},
        ensure_ascii=False) + "\n")
    logger.info(f"Delvetown act: {action} {label}")
    return {"content": msg}
