#!/usr/bin/env python3
"""Check a Discord bot token before wiring a resident to it.

Why this exists: the ways Discord bot setup fails are mostly INVISIBLE at
runtime. The worst one — the Message Content intent left off — produces a
bot that connects fine, joins the server fine, and receives every message
with `content=""`. Nothing errors. Nothing logs. The room simply looks
permanently silent, and the resident looks broken or asleep.

The good news is that it is cheaply checkable. Discord refuses the gateway
handshake outright if you REQUEST a privileged intent you have not enabled,
raising PrivilegedIntentsRequired. So connecting with the intents the
harness needs is itself the proof they are on.

This script distinguishes the failure modes that otherwise look alike:

    no token file         -> you have not saved it yet
    LoginFailure          -> the token is wrong, stale, or was reset
    PrivilegedIntents     -> the token is FINE; the intents are off
    zero guilds           -> the token is fine, the bot is not invited
    channel, no send perm -> invited, but muted in that channel

Usage:
    uv run python scripts/discord_preflight.py
    uv run python scripts/discord_preflight.py --token-file /some/path
"""

import sys
import asyncio
import argparse
from pathlib import Path

DEFAULT_TOKEN_FILE = Path.home() / ".kestrel-discord-token"

# What glm_resident/discord_backend.py requests. Kept in sync deliberately:
# a preflight that checks weaker intents than the harness uses would pass
# and then the harness would fail.
NEEDED_INTENTS = ("message_content", "members")


def read_token(path: Path) -> str:
    if not path.exists():
        print(f"✗ No token file at {path}")
        print()
        print("  Create the application and bot first (see the walkthrough),")
        print("  then save the token WITHOUT pasting it into a chat:")
        print()
        print(f"    (umask 077; pbpaste > {path})     # from your clipboard")
        print(f"    chmod 600 {path}")
        sys.exit(1)
    token = path.read_text().strip()
    if not token:
        print(f"✗ {path} exists but is empty.")
        sys.exit(1)
    mode = oct(path.stat().st_mode & 0o777)
    if path.stat().st_mode & 0o077:
        print(f"⚠ {path} is mode {mode} — readable by others. "
              f"`chmod 600 {path}`")
    # A bot token is three dot-separated parts; a common mistake is pasting
    # the application's CLIENT SECRET or its public id instead.
    if token.count(".") != 2:
        print(f"⚠ That does not look like a bot token ({token.count('.') + 1} "
              f"dot-separated parts, expected 3).")
        print("  A bot token comes from the application's **Bot** tab "
              "(Reset Token),")
        print("  not from OAuth2 (that page has the client id and secret).")
    return token


async def probe(token: str) -> int:
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.reactions = True

    client = discord.Client(intents=intents)
    result = {"code": 1}

    @client.event
    async def on_ready():
        try:
            print(f"✓ Logged in as {client.user} (id={client.user.id})")
            print("✓ Privileged intents accepted: "
                  f"{', '.join(NEEDED_INTENTS)}")
            print("  (Discord would have refused the handshake otherwise, so")
            print("   this is real proof, not a guess.)")
            print()

            guilds = client.guilds
            if not guilds:
                print("✗ The bot is in NO servers. The token works; it just "
                      "has not been invited.")
                print("  Use the invite URL from the walkthrough.")
                result["code"] = 2
                return

            print(f"✓ In {len(guilds)} server(s):")
            usable_total = 0
            for g in guilds:
                print(f"\n  {g.name}  (guild id {g.id})")
                me = g.me
                readable = []
                for ch in g.text_channels:
                    p = ch.permissions_for(me)
                    flags = []
                    if not p.view_channel:
                        continue          # invisible to him; not worth listing
                    if not p.read_message_history:
                        flags.append("no history")
                    if not p.send_messages:
                        flags.append("CANNOT SEND")
                    if not p.add_reactions:
                        flags.append("no reactions")
                    if not p.embed_links:
                        flags.append("no embeds (links render bare)")
                    readable.append((ch.name, flags))
                if not readable:
                    print("    (no channels visible to him — check channel "
                          "overrides)")
                for name, flags in readable:
                    mark = "ok  " if not flags else "warn"
                    note = "" if not flags else "  — " + ", ".join(flags)
                    print(f"    [{mark}] #{name}  -> dc-{name}{note}")
                    if not flags:
                        usable_total += 1
                print(f"\n    threads visible: "
                      f"{sum(len(c.threads) for c in g.text_channels)}")

            print()
            if usable_total:
                print(f"✓ {usable_total} channel(s) fully usable. Bring him up "
                      f"with:")
                first = guilds[0]
                print(f"    uv run python -m glm_resident --discord-only \\")
                print(f"        --discord-guild {first.id} \\")
                print(f"        --discord-channel <name>")
                print()
                print("  Channel names are given WITHOUT the dc- prefix; his")
                print("  memory adds it (config.DISCORD_STREAM_PREFIX).")
                result["code"] = 0
            else:
                print("✗ No channel is fully usable — see the warnings above.")
                result["code"] = 3
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure:
        print("✗ Login failed: Discord rejected the token.")
        print("  Most likely it was reset (resetting invalidates the old one),")
        print("  or what got saved is the client secret rather than the bot")
        print("  token. Regenerate on the Bot tab with Reset Token.")
        return 1
    except discord.PrivilegedIntentsRequired:
        print("✗ The TOKEN IS FINE. The privileged intents are not enabled.")
        print()
        print("  This is the failure that is otherwise invisible: without")
        print("  Message Content the bot connects and receives every message")
        print("  with empty content, so the room looks silent and nothing")
        print("  errors anywhere.")
        print()
        print("  Fix: developer portal -> your application -> Bot ->")
        print("  Privileged Gateway Intents -> turn ON both")
        print("    • MESSAGE CONTENT INTENT")
        print("    • SERVER MEMBERS INTENT")
        print("  then Save Changes and re-run this script.")
        return 4
    except Exception as e:
        print(f"✗ Unexpected failure: {type(e).__name__}: {e}")
        return 1
    finally:
        # On a failed login the client never finished opening, so its
        # aiohttp connector is left dangling and prints an "Unclosed
        # connector" warning AFTER our verdict — which reads like a second,
        # unrelated problem. Close it so the diagnosis is the last word.
        if not client.is_closed():
            await client.close()
    return result["code"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    args = ap.parse_args()

    print(f"Discord preflight — {args.token_file}")
    print("=" * 60)
    token = read_token(args.token_file)
    code = asyncio.run(probe(token))
    print("=" * 60)
    print("PASS — ready to run him on Discord." if code == 0
          else f"FAIL (exit {code}) — see above.")
    sys.exit(code)


if __name__ == "__main__":
    main()
