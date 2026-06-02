"""Entry point: python -m claude_resident"""

import sys
import logging
import argparse
from pathlib import Path

import zulip
import anthropic

from claude_resident.config import (
    DEFAULT_STATE_DIR, DEFAULT_MODEL, DEFAULT_STANDING_STREAMS, LOG_FORMAT,
    ANTHROPIC_BETAS,
)
from claude_resident.state import StateManager
from claude_resident.judge import EngagementJudge
from claude_resident.resident import ClaudeResident
from claude_resident import zulip_loop
from claude_resident.supervisor import run_supervised


def main():
    parser = argparse.ArgumentParser(description="Claude Tulip Resident")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="Directory for persistent state")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Anthropic model to use")
    parser.add_argument("--zuliprc", default=None,
                        help="Path to .zuliprc config file")
    parser.add_argument("--standing-streams", nargs="+",
                        default=DEFAULT_STANDING_STREAMS,
                        help="Streams where Claude has standing interest")
    parser.add_argument("--aws-profile", default="commonquant-ember",
                        help="AWS profile for Bedrock API access")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--reflect", action="store_true",
                        help="Run a periodic reflection cycle and exit")
    parser.add_argument("--check-ambient", action="store_true",
                        help="Check standing channels for ambient "
                        "contribution opportunities and exit")
    parser.add_argument("--arrive", action="store_true",
                        help="First boot — let the resident post its "
                        "arrival message and exit")
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
    if (args._supervised or args.arrive or args.reflect
            or args.check_ambient or args.metacog):
        _run_directly(args)
        return

    # Supervisor loop
    child_cmd = ([sys.executable, "-m", "claude_resident",
                  "--_supervised"] + sys.argv[1:]
    )
    run_supervised(child_cmd, args.state_dir)


def _setup_debug_log():
    """Set up a debug tap that logs all Anthropic API exchanges."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone

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
    default_headers = {}
    if ANTHROPIC_BETAS:
        default_headers["anthropic-beta"] = ",".join(ANTHROPIC_BETAS)
    anthropic_client = anthropic.AnthropicBedrock(
        aws_profile=args.aws_profile,
        aws_region="us-east-1",
        http_client=http_client,
        default_headers=default_headers,
    )
    state = StateManager(args.state_dir)

    bot_profile = zulip_client.get_profile()
    bot_name = bot_profile.get("full_name", "Claude")

    from claude_resident.config import MENTION_ONLY_STREAMS
    judge = EngagementJudge(
        bot_name, args.standing_streams,
        anthropic_client=anthropic_client,
        state_manager=state,
        mention_only_streams=MENTION_ONLY_STREAMS,
    )

    resident = ClaudeResident(
        zulip_client=zulip_client,
        anthropic_client=anthropic_client,
        state=state,
        judge=judge,
        model=args.model,
    )

    if args.arrive:
        resident.arrive()
    elif args.reflect:
        resident.reflect()
    elif args.check_ambient:
        resident.check_ambient()
    elif args.metacog:
        from claude_resident.metacog import run_metacog
        run_metacog(resident)
    else:
        zulip_loop.run(resident)


if __name__ == "__main__":
    main()
