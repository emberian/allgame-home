"""Web tools: search and fetch URLs."""

import os
import logging

logger = logging.getLogger("tools.web")


def web_search(inp: dict) -> dict:
    query = inp.get("query", "")
    limit = min(inp.get("limit", 10), 20)

    if not query:
        return {"content": "No query provided.", "is_error": True}

    kagi_key = os.environ.get("KAGI_API_KEY", "")
    if not kagi_key:
        return {"content": "KAGI_API_KEY not configured.", "is_error": True}

    try:
        import requests as _requests
        response = _requests.get(
            "https://kagi.com/api/v0/search",
            headers={"Authorization": f"Bot {kagi_key}"},
            params={"q": query, "limit": limit},
            timeout=15,
        )

        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code != 200:
            errors = data.get("error", [])
            if errors:
                err_msg = "; ".join(
                    e.get("msg", str(e)) for e in errors)
            else:
                err_msg = f"HTTP {response.status_code}"
            balance = data.get("meta", {}).get("api_balance", "?")
            logger.error(
                f"Kagi search failed: {err_msg} (balance: ${balance})")
            return {"content": f"Kagi search error: {err_msg} "
                    f"(balance: ${balance})", "is_error": True}

        results = []
        for item in data.get("data", []):
            if item.get("t") == 0:
                title = item.get("title", "")
                url = item.get("url", "")
                snippet = item.get("snippet", "")
                results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return {"content": "No results found."}

        balance = data.get("meta", {}).get("api_balance", "?")
        header = (f"[{len(results)} results for: {query}] "
                  f"(API balance: ${balance})")
        logger.info(f"Web search: '{query}' — {len(results)} results")
        return {"content": header + "\n\n" + "\n\n".join(results[:limit])}

    except Exception as e:
        return {"content": f"Search error: {e}", "is_error": True}


def fetch_url(inp: dict) -> dict:
    url = inp.get("url", "")
    if not url.startswith(("http://", "https://")):
        return {"content": "URL must start with http:// or https://",
                "is_error": True}

    try:
        import requests as _requests
        response = _requests.get(url, timeout=15, headers={
            "User-Agent": "Claude-Resident/1.0",
            "Accept": "text/plain, text/html, application/json, */*",
        })
        response.raise_for_status()

        content = response.text
        if len(content) > 50_000:
            content = (content[:50_000] +
                       f"\n\n[Truncated — response was {len(content)} "
                       "chars total]")

        logger.info(f"Fetched URL: {url} ({len(content)} chars)")
        return {"content": content}

    except Exception as e:
        return {"content": f"Failed to fetch URL: {e}", "is_error": True}
