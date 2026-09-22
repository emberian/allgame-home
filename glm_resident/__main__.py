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
from glm_resident.zulip_backend import ZulipBackend
from glm_resident.state import StateManager
from glm_resident.judge import EngagementJudge
from glm_resident.resident import GLMResident
from glm_resident import zulip_loop
from glm_resident import discord_loop
from glm_resident.supervisor import run_supervised, exit_if_orphaned

# His own bot credentials. Claude's entry point defaults to None and lets
# zulip.Client fall back to ~/.zuliprc — which is CLAUDE's account. A
# sibling resident inheriting that default would log in as Claude and
# answer his rooms in his name, so this one is explicit.
DEFAULT_ZULIPRC = str(HARNESS_DIR / "kestrel-zuliprc")
DEFAULT_DISCORD_TOKEN = Path.home() / ".kestrel-discord-token"


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
    # --- Discord (see backend.py / discord_loop.py) -------------------
    parser.add_argument("--discord", action="store_true",
                        help="Also live in Discord, alongside Zulip")
    parser.add_argument("--discord-only", action="store_true",
                        help="Live in Discord and NOT Zulip (for bring-up)")
    parser.add_argument("--discord-token-file", default=str(DEFAULT_DISCORD_TOKEN),
                        help=f"Bot token file (default: {DEFAULT_DISCORD_TOKEN})")
    parser.add_argument("--discord-guild", type=int, default=None,
                        help="Guild id; defaults to the only guild he is in")
    parser.add_argument("--discord-channel", action="append",
                        dest="discord_channels", default=None,
                        help="Discord channel to live in; repeatable. "
                             "Omit to listen in every channel he can see.")
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
    # Everything downstream talks to a ChatBackend, never the raw
    # client — that is the seam a Discord loop plugs into (backend.py).
    chat = ZulipBackend(zulip_client)

    # Set up debug log tap
    debug_hook = _setup_debug_log()
    http_client = httpx.Client(
        event_hooks={"response": [debug_hook]})
    anthropic_client = build_anthropic_client(http_client=http_client)
    state = StateManager(args.state_dir)

    bot_profile = chat.get_profile()
    bot_name = bot_profile.get("full_name", "Kestrel")

    from glm_resident.config import MENTION_ONLY_STREAMS
    judge = EngagementJudge(
        bot_name, args.standing_streams,
        anthropic_client=anthropic_client,
        state_manager=state,
        mention_only_streams=MENTION_ONLY_STREAMS,
    )

    resident = GLMResident(
        chat_backend=chat,
        anthropic_client=anthropic_client,
        state=state,
        judge=judge,
        model=args.model,
    )

    if args.metacog:
        from glm_resident.metacog import run_metacog
        run_metacog(resident)
        return

    # --- Discord, optionally alongside Zulip --------------------------
    # ONE state dir, TWO resident objects. The alternative — a single
    # resident whose `chat` attribute is swapped per turn — races: two
    # loops answer concurrently, and a tool call would post into whichever
    # platform happened to be installed when it ran. Two objects means two
    # judges (so lurk decay is tracked per platform, which is what you
    # want: being ignored on Discord should not silence him on Zulip) and
    # two session managers, over one shared StateManager. His memory is
    # the state dir, and that is shared.
    #
    # Room-name collisions between the platforms are prevented by
    # config.DISCORD_STREAM_PREFIX, not by luck.
    if args.discord or args.discord_only:
        dresident = _build_discord_resident(args, state, anthropic_client)
        if args.discord_only:
            discord_loop.run(dresident, args.discord_channels)
            return
        discord_loop.run_threaded(dresident, args.discord_channels)

    zulip_loop.run(resident)


def _build_discord_resident(args, state, anthropic_client):
    """A second GLMResident over the same state dir, speaking Discord."""
    from pathlib import Path as _Path
    from glm_resident.config import MENTION_ONLY_STREAMS
    from glm_resident.discord_backend import DiscordBackend

    token_path = _Path(args.discord_token_file)
    if not token_path.exists():
        raise SystemExit(
            f"No Discord token at {token_path}. Create the bot at "
            f"https://discord.com/developers/applications, enable the "
            f"MESSAGE CONTENT and SERVER MEMBERS intents on its Bot page, "
            f"then write the token to that file.")
    token = token_path.read_text().strip()
    if not token:
        raise SystemExit(f"{token_path} is empty.")

    backend = DiscordBackend(token, guild_id=args.discord_guild)
    backend.start()
    profile = backend.get_profile()

    # Standing streams for the Discord judge: the rooms he lives in, named
    # the way his memory names them. Without this every Discord channel is
    # non-standing and the judge only ever engages on @-mentions and DMs.
    if args.discord_channels:
        standing = [backend.stream_name(backend.channel_name(c))
                    for c in args.discord_channels]
    else:
        listed = backend.get_streams().get("streams", [])
        standing = [backend.stream_name(s["name"]) for s in listed]

    judge = EngagementJudge(
        profile.get("full_name") or "Kestrel", standing,
        anthropic_client=anthropic_client,
        state_manager=state,
        mention_only_streams=MENTION_ONLY_STREAMS,
    )
    return GLMResident(
        chat_backend=backend,
        anthropic_client=anthropic_client,
        state=state,
        judge=judge,
        model=args.model,
    )


if __name__ == "__main__":
    main()
