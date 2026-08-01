"""Web tools: search and fetch URLs."""

import os
import re as _re
import html as _html
import base64
import logging

logger = logging.getLogger("tools.web")


def web_search(inp: dict) -> dict:
    """Kagi Search API v1.

    v0 is the legacy endpoint and draws on a prepaid credit pot that is no
    longer topped up — it fails with "Insufficient credit" and an
    api_balance of 0 no matter how the account is configured. v1 is the
    live, usage-invoiced API and needs its own key (a v0 key 401s here).

    Shape differences from v0, all of them load-bearing:
      GET  ?q=      ->  POST {"query": ...}
      Authorization: Bot   ->  Bearer
      data: [ {t:0, ...} ] ->  data.search: [ {url,title,snippet,...} ]
    """
    query = inp.get("query", "")
    limit = min(inp.get("limit", 10), 20)

    if not query:
        return {"content": "No query provided.", "is_error": True}

    kagi_key = os.environ.get("KAGI_API_KEY", "")
    if not kagi_key:
        return {"content": "KAGI_API_KEY not configured.", "is_error": True}

    try:
        import requests as _requests
        response = _requests.post(
            "https://kagi.com/api/v1/search",
            headers={"Authorization": f"Bearer {kagi_key}",
                     "Content-Type": "application/json"},
            json={"query": query},
            timeout=20,
        )

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code != 200:
            errs = data.get("errors") or []
            msg = "; ".join(e.get("message", str(e)) for e in errs) \
                or f"HTTP {response.status_code}"
            logger.error(f"Kagi search failed: {msg}")
            return {"content": f"Kagi search error: {msg}", "is_error": True}

        items = ((data.get("data") or {}).get("search")) or []
        results = []
        for item in items[:limit]:
            title = _clean(item.get("title", ""))
            url = item.get("url", "")
            snippet = _clean(item.get("snippet", ""))
            results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return {"content": "No results found."}

        logger.info(f"Web search: '{query}' — {len(results)} results")
        return {"content": f"[{len(results)} results for: {query}]\n\n"
                + "\n\n".join(results)}

    except Exception as e:
        logger.error(f"Kagi search failed: {e}")
        return {"content": f"Search error: {e}", "is_error": True}


def _clean(text: str) -> str:
    """Kagi returns HTML-escaped text with <strong> hit markers."""
    return _html.unescape(_re.sub(r"</?strong>", "", text or "")).strip()


_TEXT_CONTENT_HINTS = (
    "text/", "application/json", "application/xml", "+json", "+xml",
    "application/javascript", "application/x-yaml", "application/yaml",
    "application/csv", "application/x-www-form-urlencoded",
)


def fetch_url(inp: dict, zulip_client=None, sandbox_dir=None) -> dict:
    """Fetch any URL. Zulip links (/user_uploads/... or the Zulip host) are
    fetched with the bot's authenticated session so the resident can download
    *any* attachment, not just images. Text is returned inline; binaries are
    saved into the sandbox workspace for use with the sandbox file tools."""
    from pathlib import Path
    from urllib.parse import urljoin, urlparse, unquote

    url = inp.get("url", "")
    if not url:
        return {"content": "No url provided.", "is_error": True}

    zulip_base = getattr(zulip_client, "base_url", "") or ""
    zulip_host = urlparse(zulip_base).netloc if zulip_base else ""

    is_zulip = False
    if url.startswith("/user_uploads") or url.startswith("/api/"):
        if zulip_client is None:
            return {"content": "Relative Zulip path but no Zulip session "
                    "available.", "is_error": True}
        url = urljoin(zulip_base, url)
        is_zulip = True
    elif not url.startswith(("http://", "https://")):
        return {"content": "URL must start with http:// or https:// "
                "(or be a /user_uploads/... Zulip path)", "is_error": True}
    elif zulip_host and urlparse(url).netloc == zulip_host:
        is_zulip = True

    try:
        if is_zulip and zulip_client is not None:
            response = zulip_client.session.get(url, timeout=30)
        else:
            import requests as _requests
            response = _requests.get(url, timeout=15, headers={
                "User-Agent": "Claude-Resident/1.0",
                "Accept": "*/*",
            })
        response.raise_for_status()

        ctype = response.headers.get("content-type", "").lower()
        is_text = any(h in ctype for h in _TEXT_CONTENT_HINTS) or not ctype

        if is_text:
            content = response.text
            if len(content) > 50_000:
                content = (content[:50_000] +
                           f"\n\n[Truncated — response was {len(content)} "
                           "chars total]")
            logger.info(f"Fetched URL: {url} ({len(content)} chars, {ctype})")
            return {"content": content}

        data = response.content
        name = unquote(Path(urlparse(url).path).name) or "download.bin"
        ext = Path(name).suffix.lower()

        # PDFs and images: hand straight back as a content block in the tool
        # result. The API accepts block lists for tool_result content, so the
        # resident can read these with NO docker/sandbox dependency.
        is_pdf = "pdf" in ctype or ext == ".pdf"
        img_mt = None
        if "image/" in ctype:
            img_mt = ctype.split(";")[0].strip()
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            img_mt = "image/jpeg" if ext in (".jpg", ".jpeg") else \
                f"image/{ext.lstrip('.')}"

        if is_pdf:
            encoded = base64.b64encode(data).decode("ascii")
            if len(encoded) > 32_000_000:
                return {"content": f"PDF too large ({len(data)} bytes, "
                        "max ~24MB).", "is_error": True}
            logger.info(f"Fetched PDF: {url} ({len(data)} bytes) -> "
                        "tool_result document block")
            return {"content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": encoded}},
                {"type": "text", "text": f"Fetched `{name}` "
                 f"({len(data)} bytes) via authenticated Zulip session. "
                 "The PDF is attached above — read it directly."},
            ]}

        if img_mt:
            encoded = base64.b64encode(data).decode("ascii")
            if len(encoded) > 5_000_000:
                return {"content": f"Image too large ({len(data)} bytes).",
                        "is_error": True}
            logger.info(f"Fetched image: {url} ({len(data)} bytes) -> "
                        "tool_result image block")
            return {"content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": img_mt,
                    "data": encoded}},
                {"type": "text", "text": f"Fetched `{name}` "
                 f"({len(data)} bytes)."},
            ]}

        # Other binaries (zip, tar, xlsx, ...) still need the sandbox to be
        # useful — those genuinely require tooling to inspect.
        if sandbox_dir is None:
            return {"content": f"[binary {ctype}, {len(data)} bytes] "
                    f"Downloaded `{name}` OK, but it's not a PDF/image and "
                    "no sandbox workspace is active (docker). Run "
                    "run_sandbox, then fetch again to save it there.",
                    "is_error": True}

        safe = name.replace("/", "_").replace("..", "_")
        dest = Path(sandbox_dir) / safe
        dest.write_bytes(data)
        logger.info(f"Fetched URL: {url} -> sandbox/{safe} "
                    f"({len(data)} bytes, {ctype})")
        return {"content": f"Saved binary attachment to sandbox as `{safe}` "
                f"({len(data)} bytes, {ctype}). Use get_sandbox_file or "
                f"run_sandbox to work with it."}

    except Exception as e:
        return {"content": f"Failed to fetch URL: {e}", "is_error": True}


LM_STUDIO_BASE = "http://localhost:1234"


def lm_studio_inference(inp: dict) -> dict:
    """Run inference against the local LM Studio endpoint."""
    import json
    import requests as _requests

    prompt = inp.get("prompt", "")
    system = inp.get("system", "")
    model = inp.get("model", "")
    max_tokens = inp.get("max_tokens", 8192)
    temperature = inp.get("temperature", 0.7)

    if not prompt:
        return {"content": "No prompt provided.", "is_error": True}

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if model:
        body["model"] = model

    try:
        resp = _requests.post(
            f"{LM_STUDIO_BASE}/v1/chat/completions",
            json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        text = choice["message"]["content"]
        usage = data.get("usage", {})
        model_used = data.get("model", "unknown")
        meta = (f"[model={model_used}, "
                f"tokens={usage.get('completion_tokens', '?')}/"
                f"{usage.get('total_tokens', '?')}]")
        logger.info(f"LM Studio inference: {meta}")
        return {"content": f"{text}\n\n---\n{meta}"}
    except _requests.ConnectionError:
        return {"content": "Cannot connect to LM Studio at localhost:1234. "
                "Is it running?", "is_error": True}
    except Exception as e:
        return {"content": f"LM Studio error: {e}", "is_error": True}
