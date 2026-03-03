"""Entry point: python -m claude_resident"""

import sys
import logging
import argparse
from pathlib import Path

import zulip
import anthropic

from claude_resident.config import (
    DEFAULT_STATE_DIR, DEFAULT_MODEL, DEFAULT_STANDING_STREAMS, LOG_FORMAT,
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


def _run_directly(args):
    """Run the event loop directly (as supervised child or one-shot)."""
    from dotenv import load_dotenv
    load_dotenv()

    zulip_kwargs = {}
    if args.zuliprc:
        zulip_kwargs["config_file"] = args.zuliprc
    zulip_client = zulip.Client(**zulip_kwargs)
    anthropic_client = anthropic.Anthropic()
    state = StateManager(args.state_dir)

    bot_profile = zulip_client.get_profile()
    bot_name = bot_profile.get("full_name", "Claude")

    judge = EngagementJudge(
        bot_name, args.standing_streams,
        anthropic_client=anthropic_client,
        state_manager=state,
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
