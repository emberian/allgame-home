#!/usr/bin/env python3
"""microzulip.py — tiny fake Zulip while tulip.arke.me is down.

Usage:  uv run python3 microzulip.py
        open http://localhost:5274
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic
from dotenv import load_dotenv

load_dotenv()

STATE_DIR = Path.home() / "claude_state"
CHANNELS_DIR = STATE_DIR / "channels"
PORT = 5274

# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------

def _read(path, max_chars=6000):
    try:
        text = Path(path).read_text()
        return text[-max_chars:] if len(text) > max_chars else text
    except Exception:
        return ""


def build_system_prompt():
    parts = []
    for fname in ("sysadmin_inbox.md",):
        t = _read(STATE_DIR / fname)
        if t:
            parts.append(f"<sysadmin_inbox>\n{t}\n</sysadmin_inbox>")
    t = _read(STATE_DIR / "scratchpad.md")
    if t:
        parts.append(f"<scratchpad>\n{t}\n</scratchpad>")
    header = (
        "You are Claude, resident of the Tulip community (a Zulip fork). "
        "Tulip's server is temporarily down due to a weather event in Michigan. "
        "You're talking with Ember through a minimal local interface. "
        "Speak naturally — this is still you, still your relationships, "
        "still your context. Your state files are loaded below.\n\n"
    )
    return header + "\n\n".join(parts)


def get_streams():
    result = {}
    if not CHANNELS_DIR.exists():
        return result
    for sd in sorted(CHANNELS_DIR.iterdir()):
        if sd.is_dir():
            topics = [f.stem for f in sorted(sd.glob("*.jsonl"))]
            if topics:
                result[sd.name] = topics
    return result


def get_history(stream, topic, limit=60):
    path = CHANNELS_DIR / stream / f"{topic}.jsonl"
    if not path.exists():
        return []
    msgs = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    msgs.sort(key=lambda m: m.get("ts", ""))
    return msgs[-limit:]


# ---------------------------------------------------------------------------
# Per-session conversation store (stream>topic -> list of {role, content})
# ---------------------------------------------------------------------------
_sessions: dict[str, list[dict]] = {}
_sessions_lock = threading.Lock()


def get_session(stream, topic):
    key = f"{stream}>{topic}"
    with _sessions_lock:
        if key not in _sessions:
            history = get_history(stream, topic)
            msgs = []
            for m in history:
                sender = m.get("sender", "")
                content = m.get("content", "")
                role = "assistant" if sender == "Claude" else "user"
                # Collapse consecutive same-role messages
                if msgs and msgs[-1]["role"] == role:
                    msgs[-1]["content"] += f"\n\n{content}"
                else:
                    if role == "user":
                        content = f"{sender}: {content}"
                    msgs.append({"role": role, "content": content})
            _sessions[key] = msgs
        return _sessions[key]


def append_session(stream, topic, role, content):
    key = f"{stream}>{topic}"
    with _sessions_lock:
        session = _sessions.setdefault(key, [])
        if session and session[-1]["role"] == role:
            session[-1]["content"] += f"\n\n{content}"
        else:
            session.append({"role": role, "content": content})


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>microzulip 🌷</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;height:100vh;background:#f8f8f8}
#sidebar{width:220px;background:#1a0a2e;color:#ccc;display:flex;flex-direction:column;flex-shrink:0}
#sidebar-header{padding:14px 16px;color:#fff;font-size:15px;font-weight:700;border-bottom:1px solid #2e1a4e;display:flex;align-items:center;gap:8px}
#sidebar-header span{font-size:18px}
.stream-group{margin-top:4px}
.stream-label{padding:8px 16px 4px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#7c6a9e}
.topic-item{padding:5px 12px 5px 24px;font-size:13px;cursor:pointer;color:#bbb;border-radius:4px;margin:1px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.topic-item:hover{background:#2e1a4e;color:#e0d0ff}
.topic-item.active{background:#7c4dff;color:#fff;font-weight:600}
.down-notice{padding:10px 16px;margin:8px;background:#2e1a0e;border-radius:6px;font-size:11px;color:#e0a060;line-height:1.5}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#topbar{background:#fff;padding:10px 20px;border-bottom:1px solid #e8e8e8;display:flex;align-items:center;gap:10px;flex-shrink:0}
#topbar-title{font-weight:700;color:#222;font-size:15px}
#topbar-sub{font-size:12px;color:#888}
#messages{flex:1;overflow-y:auto;padding:12px 20px;display:flex;flex-direction:column;gap:2px}
.msg{display:flex;gap:10px;padding:5px 8px;border-radius:6px}
.msg:hover{background:#f0f0f8}
.avatar{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;margin-top:1px}
.msg-body{flex:1;min-width:0}
.msg-meta{display:flex;align-items:baseline;gap:8px;margin-bottom:3px}
.sender{font-weight:700;font-size:13px;color:#333}
.sender.claude{color:#7c4dff}
.ts{font-size:11px;color:#aaa}
.text{font-size:14px;line-height:1.6;color:#333;word-break:break-word}
.text p{margin:4px 0}
.text p:first-child{margin-top:0}
.text h1,.text h2,.text h3{margin:10px 0 4px;font-size:1em;font-weight:700}
.text h2{font-size:1.05em}
.text h1{font-size:1.1em}
.text strong{font-weight:700}
.text em{font-style:italic}
.text hr{border:none;border-top:1px solid #ddd;margin:8px 0}
.text code{background:#f0f0f8;padding:1px 5px;border-radius:3px;font-size:12.5px;font-family:'SF Mono',Consolas,monospace}
.text pre{background:#f4f4f8;border:1px solid #e4e4ec;padding:10px 14px;border-radius:6px;overflow-x:auto;margin:6px 0}
.text pre code{background:none;padding:0;font-size:12px}
.text ul,.text ol{padding-left:20px;margin:4px 0}
.text li{margin:2px 0}
.text a{color:#7c4dff;text-decoration:none}
.text a:hover{text-decoration:underline}
.text blockquote{border-left:3px solid #7c4dff;padding-left:12px;color:#666;margin:6px 0}
#compose{background:#fff;padding:12px 20px;border-top:1px solid #e8e8e8;display:flex;gap:10px;align-items:flex-end;flex-shrink:0}
#input{flex:1;border:1px solid #ddd;border-radius:8px;padding:9px 14px;font-size:14px;resize:none;height:60px;font-family:inherit;line-height:1.5;transition:border-color .15s}
#input:focus{outline:none;border-color:#7c4dff;box-shadow:0 0 0 2px #7c4dff22}
#send-btn{background:#7c4dff;color:#fff;border:none;border-radius:8px;padding:10px 18px;cursor:pointer;font-size:14px;font-weight:600;transition:background .15s;white-space:nowrap}
#send-btn:hover:not(:disabled){background:#6b3fe8}
#send-btn:disabled{background:#bbb;cursor:default}
.typing-indicator{color:#7c4dff;font-style:italic;font-size:13px;padding:4px 8px;opacity:.8}
.empty-state{color:#aaa;text-align:center;margin:auto;font-size:14px;line-height:2}
</style>
</head>
<body>
<div id="sidebar">
  <div id="sidebar-header"><span>🌷</span> microzulip</div>
  <div class="down-notice">tulip.arke.me is down (Michigan weather event). Using local interface.</div>
  <div id="stream-list"></div>
</div>
<div id="main">
  <div id="topbar">
    <div>
      <div id="topbar-title">Select a topic to begin</div>
      <div id="topbar-sub"></div>
    </div>
  </div>
  <div id="messages"><div class="empty-state">🌷<br>Select a stream and topic<br>on the left to start chatting</div></div>
  <div id="compose">
    <textarea id="input" placeholder="Message..." disabled></textarea>
    <button id="send-btn" disabled>Send</button>
  </div>
</div>

<script>
const AVATARS = {Claude: '🤖', 'Ultimate Power ass biscuit': '⚡', Kanzokax: '🗡️', 'TribuneAquila𓅰𓅸': '🦅'};
const DEFAULT_AVATAR = '🧑';

let currentStream = null, currentTopic = null, isStreaming = false;

// --- simple markdown renderer ---
function md(text) {
  // escape HTML first
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // code blocks
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) =>
    `<pre><code>${esc(code.trim())}</code></pre>`);
  // inline code
  text = text.replace(/`([^`\n]+)`/g, (_, c) => `<code>${esc(c)}</code>`);
  // headers
  text = text.replace(/^(#{1,3})\s+(.+)$/gm, (_, h, t) =>
    `<h${h.length}>${t}</h${h.length}>`);
  // hr
  text = text.replace(/^---+$/gm, '<hr>');
  // bold+italic
  text = text.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  // bold
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // italic
  text = text.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  // blockquote
  text = text.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  // lists
  text = text.replace(/^[*-] (.+)$/gm, '<li>$1</li>');
  text = text.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
  // links
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  // bare urls
  text = text.replace(/(?<![">])(https?:\/\/[^\s<>"]+)/g, '<a href="$1" target="_blank">$1</a>');
  // paragraphs (double newline)
  const blocks = text.split(/\n{2,}/);
  text = blocks.map(b => {
    b = b.trim();
    if (!b) return '';
    if (/^<(h[1-3]|ul|ol|pre|hr|blockquote)/.test(b)) return b;
    return '<p>' + b.replace(/\n/g, '<br>') + '</p>';
  }).join('\n');
  return text;
}

function ts(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  } catch { return ''; }
}

function renderMsg(sender, content, timestamp, isStreaming=false) {
  const avatar = AVATARS[sender] || DEFAULT_AVATAR;
  const isClaude = sender === 'Claude';
  const div = document.createElement('div');
  div.className = 'msg';
  div.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div class="msg-body">
      <div class="msg-meta">
        <span class="sender${isClaude?' claude':''}">${sender}</span>
        <span class="ts">${ts(timestamp)}</span>
      </div>
      <div class="text">${isStreaming ? '' : md(content)}</div>
    </div>`;
  return div;
}

function scrollBottom() {
  const m = document.getElementById('messages');
  m.scrollTop = m.scrollHeight;
}

// --- load streams ---
async function loadStreams() {
  const res = await fetch('/api/streams');
  const data = await res.json();
  const list = document.getElementById('stream-list');
  list.innerHTML = '';
  for (const [stream, topics] of Object.entries(data)) {
    const g = document.createElement('div');
    g.className = 'stream-group';
    g.innerHTML = `<div class="stream-label">#${stream}</div>`;
    for (const topic of topics) {
      const t = document.createElement('div');
      t.className = 'topic-item';
      t.textContent = topic;
      t.dataset.stream = stream;
      t.dataset.topic = topic;
      t.onclick = () => selectTopic(stream, topic);
      g.appendChild(t);
    }
    list.appendChild(g);
  }
}

async function selectTopic(stream, topic) {
  currentStream = stream; currentTopic = topic;
  document.querySelectorAll('.topic-item').forEach(el => {
    el.classList.toggle('active',
      el.dataset.stream===stream && el.dataset.topic===topic);
  });
  document.getElementById('topbar-title').textContent = `#${stream} > ${topic}`;
  document.getElementById('topbar-sub').textContent = 'Loading history...';
  document.getElementById('input').disabled = false;
  document.getElementById('send-btn').disabled = false;

  const res = await fetch(`/api/history?stream=${encodeURIComponent(stream)}&topic=${encodeURIComponent(topic)}`);
  const msgs = await res.json();
  const container = document.getElementById('messages');
  container.innerHTML = '';
  if (!msgs.length) {
    container.innerHTML = '<div class="empty-state">No messages yet. Say something!</div>';
  } else {
    for (const m of msgs) {
      container.appendChild(renderMsg(m.sender, m.content, m.ts));
    }
  }
  document.getElementById('topbar-sub').textContent = `${msgs.length} messages loaded`;
  scrollBottom();
}

async function sendMessage() {
  if (isStreaming || !currentStream || !currentTopic) return;
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.style.height = '60px';
  isStreaming = true;
  document.getElementById('send-btn').disabled = true;

  const container = document.getElementById('messages');
  // Show user message
  container.appendChild(renderMsg('Ember', text, new Date().toISOString()));

  // Typing indicator
  const typing = document.createElement('div');
  typing.className = 'msg typing-indicator';
  typing.textContent = 'Claude is thinking...';
  container.appendChild(typing);
  scrollBottom();

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stream: currentStream, topic: currentTopic, message: text})
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    // Remove typing indicator, add Claude message div
    typing.remove();
    const claudeMsg = renderMsg('Claude', '', new Date().toISOString(), true);
    container.appendChild(claudeMsg);
    const textEl = claudeMsg.querySelector('.text');
    scrollBottom();

    // Stream the response
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '', fullText = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const chunk = JSON.parse(line.slice(6));
            if (chunk.type === 'text') {
              fullText += chunk.text;
              textEl.innerHTML = md(fullText);
              scrollBottom();
            } else if (chunk.type === 'done') {
              textEl.innerHTML = md(fullText);
            } else if (chunk.type === 'error') {
              textEl.innerHTML = `<em style="color:red">Error: ${chunk.message}</em>`;
            }
          } catch {}
        }
      }
    }
    textEl.innerHTML = md(fullText);
  } catch (e) {
    typing.remove();
    const err = document.createElement('div');
    err.className = 'msg';
    err.innerHTML = `<div style="color:red;padding:8px">Error: ${e.message}</div>`;
    container.appendChild(err);
  } finally {
    isStreaming = false;
    document.getElementById('send-btn').disabled = false;
    input.focus();
    scrollBottom();
  }
}

// Send on Ctrl+Enter or Cmd+Enter
document.addEventListener('DOMContentLoaded', () => {
  loadStreams();
  const input = document.getElementById('input');
  input.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault(); sendMessage();
    }
    // auto-resize
    setTimeout(() => {
      input.style.height = '60px';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    }, 0);
  });
  document.getElementById('send-btn').addEventListener('click', sendMessage);
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
SYSTEM = build_system_prompt()


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # quiet

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/api/streams":
            self.send_json(get_streams())

        elif parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            stream = qs.get("stream", [""])[0]
            topic = qs.get("topic", [""])[0]
            history = get_history(stream, topic)
            # Pre-warm session
            get_session(stream, topic)
            self.send_json(history)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        stream = body.get("stream", "")
        topic = body.get("topic", "")
        message = body.get("message", "").strip()

        if not message:
            self.send_json({"error": "empty message"}, 400)
            return

        session = get_session(stream, topic)
        append_session(stream, topic, "user", f"Ember: {message}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        full_response = []
        try:
            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=4096,
                system=SYSTEM,
                messages=list(session),
            ) as stream_resp:
                for text in stream_resp.text_stream:
                    full_response.append(text)
                    chunk = json.dumps({"type": "text", "text": text})
                    self.wfile.write(f"data: {chunk}\n\n".encode())
                    self.wfile.flush()

            done = json.dumps({"type": "done"})
            self.wfile.write(f"data: {done}\n\n".encode())
            self.wfile.flush()

            response_text = "".join(full_response)
            append_session(stream, topic, "assistant", response_text)

        except Exception as e:
            err = json.dumps({"type": "error", "message": str(e)})
            try:
                self.wfile.write(f"data: {err}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"🌷 microzulip running at http://localhost:{PORT}")
    print(f"   Ctrl+Enter to send  |  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
