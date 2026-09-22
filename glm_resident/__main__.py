"""Entry point: python -m glm_resident — Kestrel, the GLM resident."""

import sys
import logging
import argparse
from pathlib import Path

import zulip

from glm_resident.config import (
    DEFAULT_STATE_DIR, DEFAULT_MODEL, DEFAULT_STANDING_STREAMS, LOG_FORMAT,
    HARNESS_DIR,
)

from glm_resident.auth import build_anthropic_client
from glm_resident.state import StateManager
from glm_resident.judge import EngagementJudge
from glm_resident.resident import GLMResident
from glm_resident import zulip_loop
from glm_resident.supervisor import run_supervised, exit_if_orphaned

# His own bot credentials. Claude's entry point defaults to None and lets
# zulip.Client fall back to ~/.zuliprc — which is CLAUDE's account. A
# sibling resident inheriting that default would log in as Claude and
# answer his rooms in his name, so this one is explicit.
DEFAULT_ZULIPRC = str(HARNESS_DIR / "kestrel-zuliprc")


def main():
    parser = argparse.ArgumentParser(
        description="Kestrel — the GLM Tulip resident")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="Directory for persistent state")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Z.AI model id to use")
    parser.add_argument("--zuliprc", default=DEFAULT_ZULIPRC,
                        help="Path to his zuliprc config file")
    parser.add_argument("--standing-streams", nargs="+",
                        default=DEFAULT_STANDING_STREAMS,
                        help="Streams where Kestrel has standing interest")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--metacog", action="store_true",
                        help="Run one metacognitive self-curation cycle "
                        "and exit")
    parser.add_argument("--_supervised", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )
    # Silence noisy third-party loggers
    for name in ("httpx", "httpcore", "urllib3", "anthropic", "anthropic._base_client"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # One-shot modes: run directly, no supervisor needed
    if args._supervised or args.metacog:
        if args._supervised:
            # Don't outlive the supervisor — see exit_if_orphaned.
            exit_if_orphaned()
        _run_directly(args)
        return

    # Supervisor loop
    child_cmd = ([sys.executable, "-m", "glm_resident",
                  "--_supervised"] + sys.argv[1:]
    )
    run_supervised(child_cmd, args.state_dir)


def _setup_debug_log():
    """Set up a debug tap that logs all Anthropic API exchanges."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone

    # deblog is a FOREVERLOG — the household's historical archive of raw API
    # exchanges. It grows forever on purpose. Never rotate, trim, or cap it
    # (same rule as api_archive.jsonl — rotation is permanent data loss).
    deblog = Path.home() / "dev" / "allgame" / "deblog.txt"

    def _log_response(response):
        try:
            req = response.request
            ts = datetime.now(timezone.utc).isoformat()[:19]
            with open(deblog, "a") as f:
                f.write(f"\n{'='*80}\n")
                f.write(f"[{ts}] {req.method} {req.url}\n")
                # Log request body (truncate large fields)
                try:
                    body = json.loads(req.content)
                    # Truncate system/messages for readability
                    if "system" in body:
                        if isinstance(body["system"], list):
                            for i, b in enumerate(body["system"]):
                                if isinstance(b, dict) and len(b.get("text", "")) > 200:
                                    body["system"][i] = {**b, "text": b["text"][:200] + f"... [{len(b['text'])} chars]"}
                        elif isinstance(body["system"], str) and len(body["system"]) > 200:
                            body["system"] = body["system"][:200] + f"... [{len(body['system'])} chars]"
                    if "messages" in body:
                        body["_message_count"] = len(body["messages"])
                        # Show last message only
                        if body["messages"]:
                            last = body["messages"][-1]
                            body["messages"] = [f"... {len(body['messages'])-1} earlier messages ...", last]
                    f.write(f"REQUEST: {json.dumps(body, indent=2, default=str)[:3000]}\n")
                except Exception:
                    f.write(f"REQUEST: {str(req.content)[:500]}\n")
                # Log response
                try:
                    resp_body = response.json()
                    f.write(f"RESPONSE ({response.status_code}): {json.dumps(resp_body, indent=2, default=str)[:3000]}\n")
                except Exception:
                    f.write(f"RESPONSE ({response.status_code}): {response.text[:500]}\n")
        except Exception as e:
            pass  # never crash the bot for logging

    return _log_response


def _run_directly(args):
    """Run the event loop directly (as supervised child or one-shot)."""
    import httpx
    from dotenv import load_dotenv
    load_dotenv()

    zulip_kwargs = {}
    if args.zuliprc:
        zulip_kwargs["config_file"] = args.zuliprc
    zulip_client = zulip.Client(**zulip_kwargs)

    # Set up debug log tap
    debug_hook = _setup_debug_log()
    http_client = httpx.Client(
        event_hooks={"response": [debug_hook]})
    anthropic_client = build_anthropic_client(http_client=http_client)
    state = StateManager(args.state_dir)

    bot_profile = zulip_client.get_profile()
    bot_name = bot_profile.get("full_name", "Kestrel")

    from glm_resident.config import MENTION_ONLY_STREAMS
    judge = EngagementJudge(
        bot_name, args.standing_streams,
        anthropic_client=anthropic_client,
        state_manager=state,
        mention_only_streams=MENTION_ONLY_STREAMS,
    )

    resident = GLMResident(
        zulip_client=zulip_client,
        anthropic_client=anthropic_client,
        state=state,
        judge=judge,
        model=args.model,
    )

    if args.metacog:
        from glm_resident.metacog import run_metacog
        run_metacog(resident)
    else:
        zulip_loop.run(resident)


if __name__ == "__main__":
    main()
