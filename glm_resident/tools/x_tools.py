"""X/Twitter API tools — read-only access via bearer token."""

import os
import re
import logging
from urllib.parse import quote

logger = logging.getLogger("tools.x")

_BEARER = None

def _get_bearer():
    global _BEARER
    if _BEARER is None:
        _BEARER = os.environ.get("X_BEARER_TOKEN", "")
    return _BEARER


def _x_get(endpoint: str, params: dict | None = None) -> dict:
    """Make an authenticated GET request to the X API v2."""
    import requests as _requests

    bearer = _get_bearer()
    if not bearer:
        return {"error": "X_BEARER_TOKEN not configured."}

    resp = _requests.get(
        f"https://api.x.com/2/{endpoint}",
        headers={"Authorization": f"Bearer {bearer}"},
        params=params or {},
        timeout=15,
    )

    if resp.status_code == 429:
        reset = resp.headers.get("x-rate-limit-reset", "?")
        return {"error": f"Rate limited. Resets at {reset}."}
    if resp.status_code == 403:
        return {"error": "403 Forbidden — your API tier may not include this endpoint."}
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        return {"error": f"X API {resp.status_code}: {detail}"}

    return resp.json()


def _extract_tweet_id(url_or_id: str) -> str | None:
    """Extract tweet ID from a URL or bare ID."""
    url_or_id = url_or_id.strip()
    if url_or_id.isdigit():
        return url_or_id
    m = re.search(r'(?:twitter\.com|x\.com)/\w+/status/(\d+)', url_or_id)
    return m.group(1) if m else None


# Standard tweet fields for rich output
_TWEET_FIELDS = "created_at,public_metrics,author_id,conversation_id,in_reply_to_user_id,referenced_tweets,entities"
_USER_FIELDS = "name,username,verified,public_metrics,description"
_EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id"


def _format_user(user: dict) -> str:
    name = user.get("name", "")
    handle = user.get("username", "")
    verified = " ✓" if user.get("verified") else ""
    metrics = user.get("public_metrics", {})
    followers = metrics.get("followers_count", 0)
    return f"{name} @{handle}{verified} ({followers:,} followers)"


def _format_tweet(tweet: dict, users: dict[str, dict] | None = None,
                  ref_tweets: dict[str, dict] | None = None) -> str:
    """Format a single tweet for display."""
    lines = []

    # Author
    author_id = tweet.get("author_id", "")
    if users and author_id in users:
        lines.append(_format_user(users[author_id]))
    elif author_id:
        lines.append(f"[user {author_id}]")

    # Timestamp
    created = tweet.get("created_at", "")
    if created:
        lines.append(created)

    # Reply/quote context
    refs = tweet.get("referenced_tweets", [])
    for ref in refs:
        ref_type = ref.get("type", "")
        ref_id = ref.get("id", "")
        if ref_type == "replied_to":
            lines.append(f"↩ replying to tweet {ref_id}")
        elif ref_type == "quoted":
            lines.append(f"🔁 quoting tweet {ref_id}")
            if ref_tweets and ref_id in ref_tweets:
                qt = ref_tweets[ref_id]
                lines.append(f"  > {qt.get('text', '')[:200]}")
        elif ref_type == "retweeted":
            lines.append(f"♻ retweet of {ref_id}")

    # Text
    lines.append("")
    lines.append(tweet.get("text", ""))

    # Metrics
    m = tweet.get("public_metrics", {})
    parts = []
    for key, label in [("like_count", "♥"), ("retweet_count", "♻"),
                       ("reply_count", "💬"), ("impression_count", "👁")]:
        val = m.get(key, 0)
        if val:
            parts.append(f"{label} {val:,}")
    if parts:
        lines.append("  ".join(parts))

    lines.append(f"https://x.com/i/status/{tweet.get('id', '')}")
    return "\n".join(lines)


def _build_includes(data: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """Extract user and tweet lookup dicts from API includes."""
    users = {}
    ref_tweets = {}
    includes = data.get("includes", {})
    for u in includes.get("users", []):
        users[u["id"]] = u
    for t in includes.get("tweets", []):
        ref_tweets[t["id"]] = t
    return users, ref_tweets


def read_tweet(inp: dict) -> dict:
    """Look up a tweet by URL or ID."""
    tweet_ref = inp.get("tweet", "")
    if not tweet_ref:
        return {"content": "No tweet URL or ID provided.", "is_error": True}

    tweet_id = _extract_tweet_id(tweet_ref)
    if not tweet_id:
        return {"content": f"Could not extract tweet ID from: {tweet_ref}",
                "is_error": True}

    data = _x_get(f"tweets/{tweet_id}", {
        "tweet.fields": _TWEET_FIELDS,
        "user.fields": _USER_FIELDS,
        "expansions": _EXPANSIONS,
    })

    if "error" in data:
        return {"content": data["error"], "is_error": True}

    tweet = data.get("data")
    if not tweet:
        errors = data.get("errors", [])
        if errors:
            return {"content": f"Tweet not found: {errors[0].get('detail', errors)}",
                    "is_error": True}
        return {"content": "Tweet not found.", "is_error": True}

    users, ref_tweets = _build_includes(data)
    formatted = _format_tweet(tweet, users, ref_tweets)
    logger.info(f"Read tweet {tweet_id}")
    return {"content": formatted}


def search_tweets(inp: dict) -> dict:
    """Search recent tweets (last 7 days)."""
    query = inp.get("query", "")
    if not query:
        return {"content": "No search query provided.", "is_error": True}

    limit = min(inp.get("limit", 10), 100)

    data = _x_get("tweets/search/recent", {
        "query": query,
        "max_results": max(limit, 10),  # API minimum is 10
        "tweet.fields": _TWEET_FIELDS,
        "user.fields": _USER_FIELDS,
        "expansions": _EXPANSIONS,
    })

    if "error" in data:
        return {"content": data["error"], "is_error": True}

    tweets = data.get("data", [])
    if not tweets:
        return {"content": f"No tweets found for: {query}"}

    users, ref_tweets = _build_includes(data)
    results = []
    for tweet in tweets[:limit]:
        results.append(_format_tweet(tweet, users, ref_tweets))

    meta = data.get("meta", {})
    count = meta.get("result_count", len(tweets))
    header = f"[{count} results for: {query}]"
    logger.info(f"Tweet search: '{query}' — {count} results")
    return {"content": header + "\n\n" + "\n---\n".join(results)}


def get_user_tweets(inp: dict) -> dict:
    """Get a user's recent tweets."""
    username = inp.get("username", "").lstrip("@").strip()
    if not username:
        return {"content": "No username provided.", "is_error": True}

    limit = min(inp.get("limit", 10), 100)

    # First resolve username to user ID
    user_data = _x_get(f"users/by/username/{quote(username)}", {
        "user.fields": _USER_FIELDS,
    })

    if "error" in user_data:
        return {"content": user_data["error"], "is_error": True}

    user = user_data.get("data")
    if not user:
        return {"content": f"User @{username} not found.", "is_error": True}

    user_id = user["id"]
    user_info = _format_user(user)

    # Fetch their tweets
    data = _x_get(f"users/{user_id}/tweets", {
        "max_results": max(limit, 5),  # API minimum is 5
        "tweet.fields": _TWEET_FIELDS,
        "user.fields": _USER_FIELDS,
        "expansions": _EXPANSIONS,
    })

    if "error" in data:
        return {"content": f"{user_info}\n\nError fetching tweets: {data['error']}",
                "is_error": True}

    tweets = data.get("data", [])
    if not tweets:
        return {"content": f"{user_info}\n\nNo recent tweets."}

    users, ref_tweets = _build_includes(data)
    users[user_id] = user  # ensure the author is in the lookup

    results = []
    for tweet in tweets[:limit]:
        results.append(_format_tweet(tweet, users, ref_tweets))

    logger.info(f"User tweets: @{username} — {len(tweets)} tweets")
    return {"content": f"{user_info}\n\n" + "\n---\n".join(results)}
