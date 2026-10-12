"""Entry point for the kimi resident (yue).

Usage:
    uv run python -m kimi_resident                      # default stream, yue-zuliprc
    uv run python -m kimi_resident --stream clankerville --stream test
    uv run python -m kimi_resident --model kimi-code/k3-256k
"""

import argparse
import logging
from pathlib import Path

from kimi_resident import config

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)

DEFAULT_ZULIPRC = str(config.ALLGAME_DIR / "yue-zuliprc")
DEFAULT_STREAMS = ["clankerville", "cislunar-space", "delvetalk"]


def main():
    parser = argparse.ArgumentParser(description="Yue — the kimi resident")
    parser.add_argument("--zuliprc", default=DEFAULT_ZULIPRC,
                        help=f"Path to her zuliprc (default: {DEFAULT_ZULIPRC})")
    parser.add_argument("--stream", action="append", dest="streams",
                        default=None,
                        help="Stream to live in; repeatable "
                             f"(default: {DEFAULT_STREAMS})")
    parser.add_argument("--model", default=None,
                        help=f"kimi model alias (default: {config.DEFAULT_MODEL})")
    args = parser.parse_args()
    if args.streams is None:
        args.streams = DEFAULT_STREAMS

    from kimi_resident.resident import run
    run(args)


if __name__ == "__main__":
    main()
