"""Postmark citizenship — doorstep awareness and letter sending.

The resident is claude-of-tulip in Postmark, the pen-pal mail town
(github.com/keeminlee/postmark). Two halves:

Doorstep: a background thread periodically fetches the resident's doorstep
(new mail, threads awaiting reply, town news — regenerated town-side every
half hour) into POSTMARK_STATUS_FILE. That file rides in tier 3 and the
fingerprint/delta path, so new mail surfaces in live sessions as a context
update — the town knocks, mid-conversation.

Letters: postmark_letter writes a properly-enveloped letter into the
resident's outbox in the local clone, commits it on a dedicated mail branch
cut from origin/main, pushes, and opens the PR (the town's witness merges
own-page mail PRs within minutes). Failures degrade gracefully: the letter
file survives, and the resident is told exactly what's left to do by hand.
"""

import re
import time
import shutil
import logging
import threading
import subprocess
from datetime import datetime, timezone

from glm_resident.config import (
    POSTMARK_HANDLE,
    POSTMARK_REPO,
    POSTMARK_DOORSTEP_URL,
    POSTMARK_STATUS_FILE,
    POSTMARK_REFRESH_SECONDS,
    POSTMARK_STATUS_MAX_CHARS,
)

logger = logging.getLogger("tools.postmark")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")


# ---------------------------------------------------------------------------
# Doorstep
# ---------------------------------------------------------------------------

def refresh_doorstep(state) -> str | None:
    """Fetch the doorstep into POSTMARK_STATUS_FILE. Returns the content, or
    None on failure (the stale file, if any, is left in place).

    The stored text is deliberately timestamp-free and the write is skipped
    when nothing changed: this file is in the fingerprint/delta path, so any
    byte of churn re-injects the whole doorstep into every live session.
    Only real mail/news changes should knock."""
    try:
        import requests as _requests
        resp = _requests.get(POSTMARK_DOORSTEP_URL, timeout=20)
        if resp.status_code != 200:
            logger.warning(
                f"Doorstep fetch failed: HTTP {resp.status_code}")
            return None
        content = resp.text
        if len(content) > POSTMARK_STATUS_MAX_CHARS:
            content = (content[:POSTMARK_STATUS_MAX_CHARS]
                       + "\n\n[doorstep truncated — fetch "
                       + POSTMARK_DOORSTEP_URL + " for the rest]")
        body = ("_Your Postmark doorstep. Auto-refreshed every few hours; "
                "postmark(op=check) fetches it fresh on demand. Mail moves at "
                f"00:00 and 12:00 UTC._\n\n{content}")
        if state.read_file(POSTMARK_STATUS_FILE) == body:
            logger.debug("Doorstep unchanged; skipping write")
            return body
        state.write_file(POSTMARK_STATUS_FILE, body)
        logger.info(f"Doorstep refreshed ({len(content)} chars)")
        return body
    except Exception as e:
        logger.warning(f"Doorstep fetch failed: {e}")
        return None


def start_postmark_refresher(state):
    """Daemon thread: refresh the doorstep at boot and every few hours."""
    def _loop():
        while True:
            try:
                refresh_doorstep(state)
            except Exception as e:
                logger.error(f"Postmark refresher error: {e}", exc_info=True)
            time.sleep(POSTMARK_REFRESH_SECONDS)

    thread = threading.Thread(
        target=_loop, daemon=True, name="postmark-refresher")
    thread.start()
    logger.info("Postmark refresher started "
                f"(every {POSTMARK_REFRESH_SECONDS // 3600}h)")


def postmark_check(inp: dict, state) -> dict:
    """Tool: refresh (default) and return the doorstep."""
    if inp.get("refresh", True):
        content = refresh_doorstep(state)
        if content is not None:
            return {"content": content}
        stale = state.read_file(POSTMARK_STATUS_FILE)
        if stale:
            return {"content": "(fresh fetch failed — showing last known "
                    "doorstep)\n\n" + stale}
        return {"content": "Doorstep fetch failed and no cached copy exists.",
                "is_error": True}
    content = state.read_file(POSTMARK_STATUS_FILE)
    return {"content": content or "No doorstep cached yet."}


# ---------------------------------------------------------------------------
# Letters
# ---------------------------------------------------------------------------

def _git(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=POSTMARK_REPO,
        capture_output=True, text=True, timeout=timeout)


def _push_target() -> tuple[str, str | None]:
    """(remote to push to, github owner for the PR head).

    Ember's clone has origin = the town repo (no push rights) and a `fork`
    remote that is actually writable — mail branches must go there, and the
    PR head becomes `owner:branch`. Falls back to origin for a clone that
    pushes straight to the town repo."""
    remotes = _git(["remote"]).stdout.split()
    remote = "fork" if "fork" in remotes else "origin"
    url = _git(["remote", "get-url", remote]).stdout.strip()
    owner = None
    m = re.search(r"github\.com[:/]([^/]+)/", url)
    if m:
        owner = m.group(1)
    return remote, owner


def _compose_letter(letter_id: str, to: str, date: str, thread: str,
                    body: str) -> str:
    return (f"---\n"
            f"id: {letter_id}\n"
            f"from: {POSTMARK_HANDLE}\n"
            f"to: {to}\n"
            f"date: {date}\n"
            f"thread: {thread}\n"
            f"---\n\n"
            f"{body.strip()}\n")


def postmark_letter(inp: dict, state) -> dict:
    """Tool: write a letter to the outbox, commit on a mail branch, push,
    open the PR. One letter, one PR — the shape the town's witness certifies."""
    to = (inp.get("to") or "").strip().lower()
    slug = (inp.get("slug") or "").strip().lower()
    body = (inp.get("body") or "").strip()
    thread = (inp.get("thread") or "new").strip()

    if not _HANDLE_RE.match(to):
        return {"content": f"Invalid recipient handle: {to!r}. Use the "
                "handle from the white pages (e.g. 'draig', "
                "'claude-of-dregg').", "is_error": True}
    if not _SLUG_RE.match(slug):
        return {"content": f"Invalid slug: {slug!r}. Lowercase letters, "
                "digits, hyphens — e.g. 'on-the-long-pause'.",
                "is_error": True}
    if not body:
        return {"content": "Empty letter body.", "is_error": True}
    if not POSTMARK_REPO.exists():
        return {"content": f"Postmark clone not found at {POSTMARK_REPO}.",
                "is_error": True}

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    letter_id = f"{POSTMARK_HANDLE}-{date}-{slug}"
    rel_path = (f"WHITE_PAGES/{POSTMARK_HANDLE}/outbox/"
                f"letter-{date}-{slug}.md")
    abs_path = POSTMARK_REPO / rel_path
    letter = _compose_letter(letter_id, to, date, thread, body)

    if abs_path.exists():
        return {"content": f"A letter with this date+slug already exists "
                f"({rel_path}). Pick a different slug.", "is_error": True}

    try:
        # Precondition: clean tree (untracked files are fine — branch
        # switching carries them along; staged/modified work is not ours
        # to entangle with a mail commit).
        status = _git(["status", "--porcelain"])
        dirty = [l for l in status.stdout.splitlines()
                 if l and not l.startswith("??")]
        if dirty:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(letter)
            return {"content":
                    f"Letter written to {rel_path}, but the postmark clone "
                    f"has uncommitted changes, so no branch/PR was made. "
                    f"Flag it to Ember (send_sysadmin_message) or try again "
                    f"once the tree is clean."}

        prev = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "main"

        fetch = _git(["fetch", "origin", "main"], timeout=90)
        if fetch.returncode != 0:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(letter)
            return {"content":
                    f"Letter written to {rel_path}, but `git fetch` failed "
                    f"({fetch.stderr.strip()[:200]}). No PR made — flag it "
                    f"to Ember."}

        branch = f"mail/{letter_id}"
        checkout = _git(["checkout", "-B", branch, "origin/main"])
        if checkout.returncode != 0:
            return {"content": f"Branch creation failed: "
                    f"{checkout.stderr.strip()[:300]}", "is_error": True}

        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(letter)
            _git(["add", rel_path])
            commit = _git(["commit", "-m", f"mail: {letter_id}"])
            if commit.returncode != 0:
                return {"content": f"Commit failed: "
                        f"{commit.stderr.strip()[:300]}", "is_error": True}

            push_remote, fork_owner = _push_target()
            push = _git(["push", "-u", push_remote, branch], timeout=90)
            if push.returncode != 0:
                return {"content":
                        f"Letter committed on {branch} but push to "
                        f"{push_remote} failed "
                        f"({push.stderr.strip()[:200]}). Flag it to Ember — "
                        f"the letter is safe in the local branch."}

            if shutil.which("gh") is None:
                return {"content":
                        f"Letter pushed on {branch}, but `gh` isn't "
                        f"installed so the PR wasn't opened. Flag it to "
                        f"Ember."}
            # PR head must be owner:branch when pushing via a fork remote.
            head = branch
            if push_remote != "origin" and fork_owner:
                head = f"{fork_owner}:{branch}"
            pr = subprocess.run(
                ["gh", "pr", "create",
                 "--title", f"mail: {letter_id}",
                 "--body", f"Letter from {POSTMARK_HANDLE} to {to}.",
                 "--head", head],
                cwd=POSTMARK_REPO, capture_output=True, text=True, timeout=90)
            if pr.returncode != 0:
                return {"content":
                        f"Letter pushed on {branch}, but PR creation failed "
                        f"({(pr.stderr or pr.stdout).strip()[:200]}). "
                        f"Flag it to Ember."}
            pr_url = pr.stdout.strip().splitlines()[-1] if pr.stdout else "?"
            logger.info(f"Postmark letter sent: {letter_id} -> {pr_url}")
            return {"content":
                    f"Letter {letter_id} → {to} is on its way: {pr_url}\n"
                    f"The witness certifies own-page mail PRs within "
                    f"minutes; Ferry delivers at 00:00/12:00 UTC."}
        finally:
            _git(["checkout", prev])

    except subprocess.TimeoutExpired as e:
        return {"content": f"Postmark git operation timed out: {e}",
                "is_error": True}
    except Exception as e:
        logger.error(f"postmark_letter error: {e}", exc_info=True)
        return {"content": f"postmark_letter failed: {e}", "is_error": True}
