"""Tool schema definitions for the Anthropic API."""

TOOL_DEFINITIONS = [
    {
        "name": "read_state_file",
        "description": (
            "Read a file from your state directory (~/claude_state/). "
            "Use this to access your journal, harness source, allgame state, "
            "person notes, or any other state file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path within your state directory. "
                        "Examples: 'journal.md', 'allgame/faction.md', "
                        "'people/kanzokax.md', 'harness.py'"
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
            "This is your memory — use it to remember things worth retaining."
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
        "name": "list_state_files",
        "description": (
            "List files and directories in your state directory. "
            "Use this to discover what's available before reading."
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
        "name": "get_person_notes",
        "description": (
            "Read your notes about a community member. "
            "Shortcut for read_state_file('people/{name}.md')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Person's name."
                }
            },
            "required": ["name"]
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
            "Use upload_sandbox_file to share generated files."
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
        "name": "upload_sandbox_file",
        "description": (
            "Upload a file from the sandbox workspace to Zulip. "
            "Returns a markdown link for your response. "
            "Always review files before uploading."
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
            "Fetch the text content of a URL. Use for reading gist links, "
            "pastebins, external documentation, or any other text content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (http:// or https://)."
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
        "name": "run_background",
        "description": (
            "Run a command in your persistent background container. "
            "Unlike run_sandbox (ephemeral), this container persists. "
            "Has network access, Anthropic SDK, 512MB memory, 1 CPU core. "
            "Use timeout=0 for fire-and-forget."
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
        "name": "edit_harness",
        "description": (
            "Edit your own harness source code. "
            "Performs a string replacement with git safety. "
            "Commits current state before editing, verifies parse, "
            "commits result. On success, auto-restarts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find."
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement string."
                },
                "commit_message": {
                    "type": "string",
                    "description": "Git commit message."
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Optional: specific file within the package to edit. "
                        "If omitted, auto-detects by searching all package files."
                    )
                }
            },
            "required": ["old_string", "new_string", "commit_message"]
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
