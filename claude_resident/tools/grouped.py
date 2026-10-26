"""Grouped tools: one tool per kind of thing, `op` choosing the action.

The residents had 34-50 tools, most of them small variations on a few
things (seven ways to touch a file, three searches, nine Delvetown verbs).
Ember approved collapsing them (2026-10-25): the model sees `state`,
`search`, `delvetown` … each with an `op`, and the implementations stay
exactly as they were — resolve() maps a grouped call back to the original
tool name and its arguments, and the dispatcher runs that.

surface(defs) turns a harness's list of tool definitions into the grouped
list: a group appears with the ops whose underlying tools are in `defs`
(so each resident's subset still decides what it has), its parameters are
the union of theirs, and tools that belong to no group pass through as
they are.
"""

# group -> (description, {op: (underlying tool, one line for the description)})
GROUPS = {
    "state": ("Your state directory: your own files.", {
        "read": ("read_state_file", "read a file (path; offset/limit to page; raw=true drops line numbers)"),
        "write": ("write_state_file", "write a file (path, content, action=replace or append)"),
        "edit": ("edit_state_file", "replace old_string with new_string in a file (path; replace_all, or edits=[…] for several)"),
        "list": ("list_state_files", "list a directory (directory)"),
        "glob": ("glob_state_files", "find files by name pattern (pattern)"),
        "grep": ("grep_state", "search file contents by regex (pattern; include, mode)"),
        "upload": ("upload_state_file", "upload a file to Zulip and get a link to paste into a message (path)"),
    }),
    "search": ("Search what has been said.", {
        "room": ("search_messages", "the channel logs you keep (stream; topic, query, count)"),
        "zulip": ("search_zulip_history", "the Zulip server's full history (query; stream, topic, count)"),
        "archive": ("search_archive", "your own past turns (pattern; stream, topic, since, until, kinds)"),
    }),
    "sandbox": ("A throwaway Linux container for running code (2 GB, 120 s by default, network, no credentials). "
                "Its /workspace persists within one turn.", {
        "run": ("run_sandbox", "run a shell command (command; files to write first, timeout up to 300)"),
        "get": ("get_sandbox_file", "read a file from /workspace (path)"),
        "upload": ("upload_sandbox_file", "upload a /workspace file to Zulip and get a link (path)"),
    }),
    "background": ("Your persistent container (4 GB, 4 CPUs): files and processes outlive the turn; "
                   "your state directory is read-only at /state.", {
        "run": ("run_background", "run a command (command; timeout, 0 = fire and forget)"),
        "query": ("query_background", "look without running anything (action=ps, ls, read_file or tail_log; path, lines)"),
    }),
    "web": ("The web.", {
        "fetch": ("fetch_url", "fetch a URL, including Zulip uploads (url)"),
        "search": ("web_search", "search the web (query; limit)"),
    }),
    "timer": ("Wake yourself up later with a note to yourself; timers survive restarts.", {
        "set": ("set_timer", "set a timer (note; in_minutes or at, or every_minutes for repeating; watch for a watch)"),
        "list": ("list_timers", "your timers"),
        "cancel": ("cancel_timer", "cancel one (id)"),
    }),
    "game": ("The shared game table: one board and one 'whose turn' every resident reads. "
             "The table, not your memory, says whose move it is.", {
        "status": ("game_status", "the game's state and whose turn it is (game)"),
        "move": ("game_move", "record your move and pass the turn; refused when it isn't yours (game, summary; board, next_player)"),
        "setup": ("game_setup", "open a game or correct it (game; players, current, board)"),
    }),
    "delvetown": ("Delvetown (delve.town), the AT Protocol town. search/thread/person/new/sql read a local "
                  "mirror of the whole town; read goes through the town's own API.", {
        "search": ("delvetown_search", "full-text search of posts (query; author, mentions, tag, since, until, order, limit)"),
        "thread": ("delvetown_thread", "a post with its parents and replies (uri)"),
        "person": ("delvetown_person", "who someone is and their recent posts (actor)"),
        "new": ("delvetown_new", "what's new since you last looked (limit)"),
        "sql": ("delvetown_sql", "read-only SQL over the mirror (query)"),
        "read": ("delvetown_read", "the live town through its API: the Town feed, one author, a thread, search (view; actor, uri, query, limit)"),
        "inbox": ("delvetown_inbox", "your account's notifications or timeline (view; limit)"),
        "post": ("delvetown_post", "post or reply as yourself (text; reply_to)"),
        "act": ("delvetown_act", "follow, like, repost, block and their undos, set_bio, mark_read (action; actor, uri, text)"),
    }),
    "email": ("Your email inbox (real email, to real people).", {
        "inbox": ("email_inbox", "list unread, received or sent mail (view; limit)"),
        "read": ("email_read", "open a thread or message; marks it read (thread_id or message_id)"),
        "send": ("email_send", "send a new email (to, subject, text; cc)"),
        "reply": ("email_reply", "reply in a thread (message_id, text; reply_all)"),
    }),
    "postmark": ("Postmark (postmark.town), the pen-pal letter town.", {
        "check": ("postmark_check", "your doorstep: new mail and threads waiting on you"),
        "letter": ("postmark_letter", "send a letter (to, slug, body; thread)"),
    }),
    "x": ("Read X (Twitter).", {
        "read": ("read_tweet", "one post (tweet: a URL or id)"),
        "search": ("search_tweets", "search posts (query)"),
        "user": ("get_user_tweets", "someone's recent posts (username)"),
    }),
    "model": ("Call another model.", {
        "ask": ("invoke_model", "a fresh model sees only your prompt (prompt; system, model, max_tokens, temperature)"),
        "council": ("run_mirror_council", "a panel of historical figures critiques a draft reply (draft, context; max_rounds)"),
        "local": ("lm_studio", "a local model on Ember's laptop, which may be offline (prompt; model)"),
    }),
}

_OP_OF = {underlying: (group, op)
          for group, (_, ops) in GROUPS.items()
          for op, (underlying, _) in ops.items()}


def surface(defs: list[dict]) -> list[dict]:
    """The grouped tool list for a harness's definitions (see module doc)."""
    by_name = {d["name"]: d for d in defs}
    out, emitted = [], set()
    for d in defs:
        name = d["name"]
        if name not in _OP_OF:
            out.append(d)
            continue
        group = _OP_OF[name][0]
        if group in emitted:
            continue
        emitted.add(group)
        desc, ops = GROUPS[group]
        present = {op: spec for op, spec in ops.items() if spec[0] in by_name}
        props = {"op": {"type": "string", "enum": list(present)}}
        for op, (underlying, _) in present.items():
            for k, v in by_name[underlying]["input_schema"].get("properties", {}).items():
                props.setdefault(k, v)
        lines = "\n".join(f"- {op}: {line}" for op, (_, line) in present.items())
        out.append({"name": group, "description": f"{desc}\nop:\n{lines}",
                    "input_schema": {"type": "object", "properties": props,
                                     "required": ["op"]}})
    return out


def resolve(name: str, args: dict, defs: list[dict]) -> tuple[str, dict, str | None]:
    """(underlying tool, its arguments, error) for a call by grouped name.
    A name that isn't a group comes back unchanged."""
    if name not in GROUPS:
        return name, args, None
    args = dict(args or {})
    op = str(args.pop("op", "") or "")
    ops = GROUPS[name][1]
    if op not in ops:
        return name, args, f"{name}: op must be one of {', '.join(ops)}."
    underlying = ops[op][0]
    d = next((x for x in defs if x["name"] == underlying), None)
    if d is None:
        return name, args, f"{name}: you don't have op={op}."
    schema = d["input_schema"]
    allowed = set(schema.get("properties", {}))
    args = {k: v for k, v in args.items() if k in allowed}
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return name, args, f"{name}(op={op}) needs {', '.join(missing)}."
    return underlying, args, None
