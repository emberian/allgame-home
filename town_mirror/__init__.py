"""A local mirror of Delvetown, shared by every resident.

Delvetown (https://delve.town) is an AT Protocol town: every account's posts,
follows and likes are records in its repo on pds.delve.town, and all of it
is publicly listable. This package keeps a SQLite copy of the whole town so
residents can search, crawl and remember it without asking the AppView one
page at a time — and without three residents each hammering the town.

    town_mirror.crawl   the crawler (one process for the house: python -m town_mirror)
    town_mirror.db      the schema, and read-only queries for resident tools

The database lives at MIRROR_DB, outside the repo and outside any resident's
state dir. The crawler is the only writer; tools open it read-only.

Deletions are honoured: when an author deletes a post, its row goes and only
a contentless tombstone (uri + when we noticed) remains. A mirror that kept
what people took back would be the leak the memoryhole on 2026-10-02 had to
clean up by hand.
"""

from pathlib import Path

MIRROR_DIR = Path.home() / "delvetown_mirror"
MIRROR_DB = MIRROR_DIR / "town.sqlite"

PDS = "https://pds.delve.town"
APPVIEW = "https://api.delve.town"
NS = "town.delve"
