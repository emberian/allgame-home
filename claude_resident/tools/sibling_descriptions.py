"""Claude's tool descriptions, adapted for a sibling resident.

The descriptions in definitions.py are written to Claude: his state dir by
path, "like Claude Code's Read tool", invoke_model, images that arrive with
his messages, an archive that keeps his thinking. A sibling that runs the
same implementations (Yue, through kimi_resident or tulip_ext) was shown
those words verbatim and took them as facts about herself — she once spent
a turn probing for ~/claude_state. adapt() rewrites only what is untrue for
the sibling; anything it doesn't name passes through unchanged.
"""

_ALWAYS = [
    ("Read a file from your state directory (~/claude_state/).",
     "Read a file from your state directory."),
    (", like Claude Code's Read tool —", " —"),
    (" (like Claude Code's Grep)", ""),
    ("'harness/claude_resident/tools/dispatch.py'", "'harness.py'"),
    (" (use invoke_model for nested model calls)", ""),
    ("Has network access, Anthropic SDK, ", "Has network access, "),
    (" a saved transcript, allgame state snapshots, etc.", " a saved transcript, etc."),
]

_NO_INLINE_IMAGES = [
    ("(PDFs and images posted in messages are already delivered to you "
     "automatically as content blocks — no fetch needed for those.)",
     "(Images and PDFs posted in messages are NOT shown to you "
     "automatically: fetch_url the link to see one.)"),
]

_NO_ARCHIVED_THINKING = [
    ("the append-only foreverlog of every API response you've ever produced. "
     "This is the ONLY record of your past THINKING (internal monologue never "
     "reaches Zulip), plus everything you said and every tool call.",
     "the append-only log of your turns: what you said (the last 4,000 "
     "characters of each) and which tools you called. Your thinking is not "
     "kept in it."),
    ("Use it to recover your own past reasoning: 'what was I thinking when we "
     "designed X', 'have I talked about Y before', 'what did I decide about Z'.",
     "Use it for 'what did I say about X', 'have I talked about Y before', "
     "'what did I decide about Z'."),
]


def adapt(name: str, text: str, *, images_inline: bool = False,
          thinking_archived: bool = False) -> str:
    rules = list(_ALWAYS)
    if not images_inline:
        rules += _NO_INLINE_IMAGES
    if not thinking_archived:
        rules += _NO_ARCHIVED_THINKING
    for old, new in rules:
        text = text.replace(old, new)
    return text
