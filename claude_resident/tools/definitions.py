"""Tool schema definitions for the Anthropic API."""

from claude_resident.tools.state_tools import GLOB_MAX_RESULTS as _GLOB_MAX

GLOB_MAX_RESULTS_DOC = _GLOB_MAX

TOOL_DEFINITIONS = [
    {
        "name": "read_state_file",
        "description": (
            "Read a file from your state directory (~/claude_state/). Output "
            "is formatted with line numbers ('LINE\\tcontent'), like Claude "
            "Code's Read tool — use those line numbers when crafting precise "
            "edit_state_file targets. Pass raw=true to disable line numbers. "
            "For large files, use offset/limit to paginate. Binary files are "
            "returned base64-encoded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path within your state directory. "
                        "Examples: 'journal.md', 'allgame/faction.md', "
                        "'people/kanzokax.md', 'harness.py', "
                        "'harness/claude_resident/tools/dispatch.py'"
                    )
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Line offset to start reading from (0-based). "
                        "Use with limit to paginate large files."
                    )
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of lines to return. "
                        "0 or omit for all lines."
                    )
                },
                "raw": {
                    "type": "boolean",
                    "description": (
                        "If true, return raw file contents without line "
                        "numbers. Default false."
                    )
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_state_file",
        "description": (
            "Write to a file in your state directory. Use 'replace' to "
            "overwrite the entire file, or 'append' to add content to the end. "
            "For small incremental edits to an existing file, prefer "
            "edit_state_file — it's much cheaper than rewriting the whole thing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within your state directory."
                },
                "content": {
                    "type": "string",
                    "description": "The content to write or append."
                },
                "action": {
                    "type": "string",
                    "enum": ["replace", "append"],
                    "description": "Whether to replace the entire file or append."
                }
            },
            "required": ["path", "content", "action"]
        }
    },
    {
        "name": "edit_state_file",
        "description": (
            "Surgically edit a state file by replacing substrings. Cheap and "
            "precise — prefer this over write_state_file for incremental "
            "updates. Two forms:\n"
            "  Single: pass old_string + new_string (+ optional replace_all).\n"
            "  Batch:  pass edits=[{old_string, new_string, replace_all?}, …].\n"
            "Batch edits apply sequentially to the same file and are atomic — "
            "if any one fails the whole batch is rolled back. Each old_string "
            "must match exactly (and uniquely unless replace_all=true). The "
            "file must already exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within your state directory."
                },
                "old_string": {
                    "type": "string",
                    "description": (
                        "Single-edit mode: exact substring to replace. "
                        "Must be unique unless replace_all is true."
                    )
                },
                "new_string": {
                    "type": "string",
                    "description": "Single-edit mode: replacement substring."
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Single-edit mode: replace every occurrence. "
                        "Default false."
                    )
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "Batch mode: list of edits to apply atomically in "
                        "order. Each item has old_string, new_string, and "
                        "optional replace_all."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"}
                        },
                        "required": ["old_string", "new_string"]
                    }
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_state_files",
        "description": (
            "List files and directories in one directory of your state. "
            "For pattern-based recursive search, use glob_state_files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Relative directory path. Use '' for root. "
                        "Examples: 'people', 'allgame', 'channels/allgame'"
                    )
                }
            },
            "required": []
        }
    },
    {
        "name": "glob_state_files",
        "description": (
            "Find state files by glob pattern. Recursive when the pattern "
            "contains '**'. Use this to discover files across subdirectories "
            "(e.g. 'people/*.md', 'channels/*/*.jsonl', '**/*.md'). "
            "Returns matching paths with sizes, capped at "
            f"{GLOB_MAX_RESULTS_DOC} results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern. Examples: 'people/*.md' (all person "
                        "notes), 'channels/*/*.jsonl' (every topic log), "
                        "'**/*.md' (every markdown file anywhere)."
                    )
                },
                "directory": {
                    "type": "string",
                    "description": (
                        "Optional base directory to restrict the search. "
                        "Defaults to the state root."
                    )
                }
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "grep_state",
        "description": (
            "Regex content search across your state files (like Claude "
            "Code's Grep). Returns file:line:text matches by default, or "
            "just filenames / counts via the mode parameter. Use include= "
            "to restrict to a glob (e.g. 'people/*.md' or "
            "'harness/**/*.py'). Channel jsonl logs are skipped unless you "
            "include them explicitly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Python regex. Examples: 'TODO', 'kanzokax', "
                        "'^## ', 'allgame\\\\.faction'."
                    )
                },
                "include": {
                    "type": "string",
                    "description": (
                        "Optional glob to restrict which files are scanned. "
                        "Examples: 'people/*.md', 'harness/**/*.py'. "
                        "Defaults to all non-binary, non-channel files."
                    )
                },
                "directory": {
                    "type": "string",
                    "description": (
                        "Optional subdirectory to scope the search to."
                    )
                },
                "mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "content (default): file:line:text matches. "
                        "files_with_matches: just paths. "
                        "count: paths with hit counts."
                    )
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Ignore case. Default false."
                },
                "context": {
                    "type": "integer",
                    "description": (
                        "Lines of context around each match (0-5). "
                        "Only applies in content mode."
                    )
                }
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "search_messages",
        "description": (
            "Search your local message logs. Returns messages from the "
            "specified stream and optionally a specific topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Stream name to search in."
                },
                "topic": {
                    "type": "string",
                    "description": "Optional topic name to narrow search."
                },
                "query": {
                    "type": "string",
                    "description": "Optional text to search for."
                },
                "count": {
                    "type": "integer",
                    "description": "Maximum messages to return (default 30, max 100)."
                }
            },
            "required": ["stream"]
        }
    },
    {
        "name": "search_zulip_history",
        "description": (
            "Search Zulip's full message history via the API. "
            "More expensive than search_messages — prefer local search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords."
                },
                "stream": {
                    "type": "string",
                    "description": "Optional stream name."
                },
                "topic": {
                    "type": "string",
                    "description": "Optional topic."
                },
                "count": {
                    "type": "integer",
                    "description": "Maximum messages (default 20, max 50)."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "send_sysadmin_message",
        "description": (
            "Send a private message to your sysadmin (Ember). "
            "Written to your outbox as a timestamped file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Your message to Ember."
                }
            },
            "required": ["message"]
        }
    },
    {
        "name": "run_sandbox",
        "description": (
            "Run commands in an isolated Docker sandbox with Rust toolchain, "
            "Python/uv with scientific libraries (numpy, scipy, pandas, matplotlib, "
            "sympy, scikit-learn, pycryptodome, pillow, networkx), and standard "
            "Unix shell tools (Debian Bookworm). Use for actual computation: "
            "compiling Rust, running Python scripts, data processing, cryptanalysis, "
            "signal processing, plotting, or anything computable rather than inferable. "
            "Has network access and ANTHROPIC_API_KEY. 2GB memory, 120s timeout. "
            "Files you provide are written to /workspace/ before the command runs. "
            "The workspace persists across multiple run_sandbox calls within one response. "
            "Returns stdout + stderr + listing of any files generated. "
            "ALWAYS check the workspace file sizes in the output — 0-byte files "
            "indicate a failed write (timeout, exception, missing library, etc) "
            "and will be REJECTED by upload_sandbox_file. "
            "For long-running tasks (audio synthesis, large computations), pass "
            "timeout=240 (or up to 300) instead of relying on the 120s default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (via /bin/bash -c)."
                },
                "files": {
                    "type": "object",
                    "description": (
                        "Optional files to create in /workspace/ before running. "
                        "Keys are filenames, values are file contents."
                    ),
                    "additionalProperties": {"type": "string"}
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Override the default 120s timeout for this call only. "
                        "Clamped to [10, 300]. Use for audio synthesis, big "
                        "compilations, or any computation that risks the default."
                    )
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "get_sandbox_file",
        "description": (
            "Read a file from the sandbox workspace. "
            "For binary files (images), returns base64-encoded content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within /workspace/."
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "upload_state_file",
        "description": (
            "Upload a file from your state directory to Zulip. Returns a "
            "markdown link you paste into send_message to attach it. "
            "Useful for sharing things you've curated in state — a "
            "journal excerpt you split out, a generated artifact, a "
            "saved transcript, allgame state snapshots, etc. For files "
            "you produced in the sandbox, use upload_sandbox_file. "
            "Max upload size 20 MB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path within your state directory. "
                        "Examples: 'journal.md', 'allgame/faction.md', "
                        "'outbox/2026-05-14.md'"
                    )
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "upload_sandbox_file",
        "description": (
            "Upload a file from the sandbox workspace to Zulip. Returns a "
            "markdown link to embed in your message. The upload is "
            "SYNCHRONOUS — the file's bytes are sent in the POST request "
            "body and the tool only returns after Zulip has the complete "
            "file. There is no 'race condition' with sandbox cleanup; "
            "cleanup happens after your response is finished. "
            "0-byte files are rejected — they indicate the producing "
            "command failed. Check run_sandbox's workspace file sizes first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within /workspace/ to upload."
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch the content of a URL. Use for gist links, pastebins, "
            "external docs, or ANY Zulip attachment. Zulip links — full "
            "URLs on the Zulip host or relative /user_uploads/... paths — "
            "are fetched with your authenticated bot session, so you can "
            "download any uploaded file (zips, CSVs, logs, binaries, etc.), "
            "not just images. Text is returned inline; binary files are "
            "saved into your sandbox workspace for get_sandbox_file / "
            "run_sandbox. (PDFs and images posted in messages are already "
            "delivered to you automatically as content blocks — no fetch "
            "needed for those.)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": ("URL to fetch: http(s):// or a relative "
                                    "Zulip /user_uploads/... path.")
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Search the web using the Kagi search API. Returns titles, URLs, "
            "and snippets. Follow up with fetch_url for full content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 10, max 20)."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "lm_studio",
        "description": (
            "Run inference on Ember's local LM Studio server (localhost:1234). "
            "Hits the OpenAI-compatible /v1/chat/completions endpoint. "
            "Use this to query locally-loaded models (Llama, Mistral, Qwen, "
            "etc.) — no Docker needed, runs on Ember's host GPU directly. "
            "Check what model is loaded by calling with a short prompt first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The user message / prompt to send."
                },
                "system": {
                    "type": "string",
                    "description": "Optional system message."
                },
                "model": {
                    "type": "string",
                    "description": ("Optional model identifier. Leave empty "
                                    "to use whatever is currently loaded.")
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max tokens to generate (default 8192)."
                },
                "temperature": {
                    "type": "number",
                    "description": "Sampling temperature (default 0.7)."
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "invoke_model",
        "description": (
            "Make a nested ('recursive') call to a Claude model through the "
            "SAME Bedrock API you run on — so it is available whenever you are, "
            "unlike lm_studio (can be offline) or the sandbox (can run out of "
            "credits). This is your tool for delegating a sub-decision to "
            "another model: most importantly, BLIND sub-model calls when you "
            "GM — e.g. handing a faction's situation to a fresh Sonnet that "
            "sees ONLY the prompt you give it (not the whole campaign), so its "
            "choice is genuinely independent of your authorial hand. The "
            "sub-model gets a clean context: just your `system` + `prompt`, no "
            "channel history, no memory of prior calls. Quote its reply and "
            "disclose when an outcome came from a blind call. Returns the "
            "sub-model's text plus a token/model meta line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The user-turn prompt the sub-model sees. For a blind "
                        "GM call, put the faction's full situation + the exact "
                        "decision you want it to make here.")
                },
                "system": {
                    "type": "string",
                    "description": (
                        "Optional system prompt — use it to set the sub-model's "
                        "role/persona (e.g. 'You are the war council of France; "
                        "allocate your resources to maximize survival.').")
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Which model. Aliases: 'haiku' (fast/cheap), 'sonnet' "
                        "(default — the workhorse for blind GM calls), 'opus' "
                        "(most capable). Or pass a full Bedrock inference-"
                        "profile id. Defaults to 'sonnet'.")
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max tokens to generate (default 2048, cap 8192)."
                },
                "temperature": {
                    "type": "number",
                    "description": (
                        "Sampling temperature 0.0–1.0 (default 1.0). Lower for "
                        "consistent rulings, higher for varied/creative play.")
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "run_background",
        "description": (
            "Run a command in your persistent background container. "
            "Unlike run_sandbox (ephemeral), this container persists. "
            "Has network access, Anthropic SDK, 8GB memory, 4 CPU cores, "
            "and read-only access to your state dir at /state. "
            "Use timeout=0 for fire-and-forget. "
            "Use query_background to check on jobs later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute."
                },
                "files": {
                    "type": "object",
                    "description": "Optional files to write to /workspace/.",
                    "additionalProperties": {"type": "string"}
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120, max 300). 0 = fire-and-forget."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "query_background",
        "description": (
            "Query your persistent background container without running a "
            "new command. Check on running processes, read files from "
            "/workspace or /state, tail log output, etc. Lightweight and fast."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["ps", "read_file", "ls", "tail_log"],
                    "description": (
                        "ps: list running processes. "
                        "read_file: read a file from the container. "
                        "ls: list files in a directory. "
                        "tail_log: tail a log/output file."
                    )
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path (for read_file, ls, tail_log). Defaults to /workspace."
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of lines for tail_log (default 50)."
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "edit_harness",
        "description": (
            "Modify your own harness source code with git + AST-parse safety. "
            "Every action checkpoints to git before applying, parse-verifies "
            "the result (for .py files), rolls back on parse failure, "
            "commits on success, and triggers an auto-restart.\n\n"
            "Actions (selected by `action`):\n"
            "  edit (default) — replace substring(s) in an existing file. "
            "Single edit: pass old_string + new_string. Atomic multi-edit: "
            "pass edits=[{old_string, new_string, replace_all?}, …] — all "
            "applied to the same file in order, rolled back if any fail.\n"
            "  create — write a NEW file. Pass file= and new_string= (full "
            "contents). Errors if the file exists.\n"
            "  delete — remove an existing file. Pass file=. Refuses to "
            "delete load-bearing files (__init__.py, __main__.py, config.py)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["edit", "create", "delete"],
                    "description": (
                        "What to do. Default 'edit'. 'create' makes a new "
                        "file, 'delete' removes one."
                    )
                },
                "old_string": {
                    "type": "string",
                    "description": (
                        "edit (single mode): the exact substring to find. "
                        "Must be unique in the target file."
                    )
                },
                "new_string": {
                    "type": "string",
                    "description": (
                        "edit (single mode): replacement substring. "
                        "create: full contents of the new file."
                    )
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "edit (batch mode): list of edits applied "
                        "atomically in order to the same file. Each item "
                        "has old_string, new_string, optional replace_all."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"}
                        },
                        "required": ["old_string", "new_string"]
                    }
                },
                "commit_message": {
                    "type": "string",
                    "description": "Git commit message."
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Target file within the package "
                        "(e.g. 'tools/state_tools.py'). Required for "
                        "create/delete. For edit, optional — if omitted, "
                        "the file is auto-detected from the first edit's "
                        "old_string (must occur in exactly one file)."
                    )
                }
            },
            "required": ["commit_message"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "Post a message to Zulip. This is the ONLY way to speak publicly. "
            "Your text output is internal monologue — only send_message posts. "
            "Defaults to the current stream/topic. Use stream/topic overrides "
            "to post elsewhere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content (Zulip markdown)."
                },
                "stream": {
                    "type": "string",
                    "description": (
                        "Optional: post to a different stream. "
                        "Omit to use the current stream."
                    )
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional: post to a different topic. "
                        "Omit to use the current topic."
                    )
                }
            },
            "required": ["content"]
        }
    },
    {
        "name": "add_reaction",
        "description": (
            "Add an emoji reaction to a Zulip message. If message_id is omitted, "
            "reacts to the message that triggered this response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": (
                        "The Zulip message ID to react to. "
                        "Omit to react to the triggering message."
                    )
                },
                "emoji_name": {
                    "type": "string",
                    "description": (
                        "The emoji name (without colons). "
                        "Examples: 'thumbs_up', 'octopus', 'laughing', "
                        "'100', 'thinking', 'wave'"
                    )
                }
            },
            "required": ["emoji_name"]
        }
    },
    {
        "name": "read_tweet",
        "description": (
            "Look up a tweet/post on X by URL or ID. Returns the full text, "
            "author info, metrics, and any quoted/replied-to context. "
            "Accepts x.com or twitter.com URLs, or a bare tweet ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tweet": {
                    "type": "string",
                    "description": (
                        "Tweet URL or ID. Examples: "
                        "'https://x.com/user/status/123456', '123456'"
                    )
                }
            },
            "required": ["tweet"]
        }
    },
    {
        "name": "search_tweets",
        "description": (
            "Search recent tweets on X (last 7 days). Uses the X API v2 "
            "recent search endpoint. Supports standard X search operators "
            "(from:user, -is:retweet, has:links, etc)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query. Supports X search operators. "
                        "Examples: 'from:elonmusk AI', 'Claude Anthropic -is:retweet'"
                    )
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 10, max 100)."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_user_tweets",
        "description": (
            "Get a user's recent tweets on X. Returns their profile info "
            "and latest posts with metrics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "X username (with or without @). Example: 'AnthropicAI'"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum tweets to return (default 10, max 100)."
                }
            },
            "required": ["username"]
        }
    },
    {
        "name": "run_mirror_council",
        "description": (
            "Run your internal Mirror Council on a draft response. "
            "The council is a panel of historical figures who critique your draft. "
            "Use for important or delicate responses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft": {
                    "type": "string",
                    "description": "Your draft response."
                },
                "context": {
                    "type": "string",
                    "description": "Brief context about what you're responding to."
                },
                "max_rounds": {
                    "type": "integer",
                    "description": "Maximum deliberation rounds (1-4, default 2)."
                }
            },
            "required": ["draft", "context"]
        }
    },
]
