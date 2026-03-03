#!/usr/bin/env python3
"""
monitor.py — Claude Monitor Dashboard

A DearPyGui monitoring dashboard that wraps the Claude Tulip Resident harness.
Monkey-patches key methods at runtime to intercept all state, tool calls,
API requests, and responses — while the inner Claude sees nothing unusual.

Usage:
    python monitor.py [--state-dir PATH] [--model MODEL] [--zuliprc PATH]
                      [--standing-streams STREAM ...] [--verbose]
"""

import os
import sys
import json
import time
import queue
import types
import logging
import argparse
import importlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from uuid import uuid4

from dearpygui import dearpygui as dpg

# Import the harness — we'll monkey-patch its instances, not its source
import claude_resident
from claude_resident import (
    ClaudeResident, StateManager, EngagementJudge, SessionManager,
    DEFAULT_STATE_DIR, DEFAULT_MODEL, DEFAULT_STANDING_STREAMS,
    HARNESS_PATH, LOG_FORMAT,
)

# ---------------------------------------------------------------------------
# Event types (background thread → UI thread)
# ---------------------------------------------------------------------------

@dataclass
class MonitorEvent:
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class MessageReceived(MonitorEvent):
    message_id: int = 0
    stream: str = ""
    topic: str = ""
    sender: str = ""
    content: str = ""

@dataclass
class EngagementDecision(MonitorEvent):
    stream: str = ""
    topic: str = ""
    sender: str = ""
    should_respond: bool = False
    reason: str = ""

@dataclass
class ToolCallPending(MonitorEvent):
    request_id: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    turn: int = 0
    stream: str = ""
    topic: str = ""

@dataclass
class ToolCallResult(MonitorEvent):
    request_id: str = ""
    tool_name: str = ""
    result_content: str = ""
    is_error: bool = False

@dataclass
class ApiRequestSent(MonitorEvent):
    turn: int = 0
    model: str = ""
    message_count: int = 0
    tool_count: int = 0
    system_block_count: int = 0

@dataclass
class ApiResponseReceived(MonitorEvent):
    turn: int = 0
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0

@dataclass
class ResponseReady(MonitorEvent):
    request_id: str = ""
    stream: str = ""
    topic: str = ""
    content: str = ""

@dataclass
class ResponsePosted(MonitorEvent):
    stream: str = ""
    topic: str = ""
    content: str = ""

@dataclass
class StateFileChanged(MonitorEvent):
    path: str = ""
    action: str = ""  # 'write' or 'append'
    content_preview: str = ""

@dataclass
class SessionEvent(MonitorEvent):
    stream: str = ""
    topic: str = ""
    event_type: str = ""  # 'created', 'expired', 'trimmed', 'reset'

@dataclass
class ZulipConnected(MonitorEvent):
    queue_id: str = ""

@dataclass
class ErrorEvent(MonitorEvent):
    context: str = ""
    message: str = ""

@dataclass
class LogEvent(MonitorEvent):
    level: str = ""
    source: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Decision types (UI thread → background thread)
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    action: str  # 'approve', 'modify', 'deny', 'mock'
    modified_input: Optional[dict] = None
    mock_result: Optional[str] = None
    edited_content: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# InterventionGate — thread synchronization for gating
# ---------------------------------------------------------------------------

class InterventionGate:
    """Thread-safe gate: background thread blocks, UI thread resolves."""

    def __init__(self):
        self._pending: dict[str, threading.Event] = {}
        self._decisions: dict[str, Decision] = {}
        self._lock = threading.Lock()

    def wait_for_decision(self, request_id: str, timeout: float = 300) -> Decision:
        event = threading.Event()
        with self._lock:
            self._pending[request_id] = event

        event.wait(timeout=timeout)

        with self._lock:
            self._pending.pop(request_id, None)
            return self._decisions.pop(
                request_id, Decision(action='deny', reason='timeout'))

    def resolve(self, request_id: str, decision: Decision):
        with self._lock:
            self._decisions[request_id] = decision
            if request_id in self._pending:
                self._pending[request_id].set()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending.keys())


# ---------------------------------------------------------------------------
# Log handler that emits to the event queue
# ---------------------------------------------------------------------------

class QueueLogHandler(logging.Handler):
    """Sends log records to the monitor event queue."""

    def __init__(self, event_queue: queue.Queue):
        super().__init__()
        self._queue = event_queue

    def emit(self, record):
        try:
            self._queue.put_nowait(LogEvent(
                level=record.levelname,
                source=record.name,
                message=self.format(record),
            ))
        except queue.Full:
            pass


# ---------------------------------------------------------------------------
# HarnessMonitor — monkey-patching wrapper
# ---------------------------------------------------------------------------

class HarnessMonitor:
    """Wraps a ClaudeResident instance with monitoring hooks."""

    # Tools that require gating by default
    DEFAULT_GATED_TOOLS = {
        "write_state_file", "send_sysadmin_message",
        "upload_sandbox_file", "edit_harness",
    }

    def __init__(self, resident: ClaudeResident, event_queue: queue.Queue):
        self.resident = resident
        self.events = event_queue
        self.gate = InterventionGate()
        self.auto_approve_tools = True  # start in autopilot
        self.auto_approve_responses = True
        self.gated_tools: set[str] = set(self.DEFAULT_GATED_TOOLS)
        self._response_in_flight = False  # flag to distinguish Claude responses
        self._current_turn = 0
        self._current_stream = ""
        self._current_topic = ""
        self._apply_patches()

    def _apply_patches(self):
        """Monkey-patch the resident instance with monitoring hooks."""
        self._patch_handle_message()
        self._patch_execute_tool()
        self._patch_post_response()
        self._patch_api_create()
        self._patch_state_writes()
        self._patch_state_log()
        self._patch_engagement()
        self._patch_sessions()
        self._patch_run()

    # --- Patch: handle_message ---
    def _patch_handle_message(self):
        original = self.resident.handle_message
        monitor = self

        def patched(message):
            msg_type = message.get("type", "stream")
            if msg_type == "stream":
                stream = message.get("display_recipient", "unknown")
                topic = message.get("subject", "")
            else:
                stream = "dm"
                topic = message.get("sender_email", "unknown")
            sender = message.get("sender_full_name", "unknown")
            content = message.get("content", "")

            monitor.events.put(MessageReceived(
                message_id=message.get("id", 0),
                stream=stream, topic=topic,
                sender=sender, content=content,
            ))

            monitor._current_stream = stream
            monitor._current_topic = topic
            monitor._response_in_flight = True
            try:
                original(message)
            finally:
                monitor._response_in_flight = False

        self.resident.handle_message = patched

    # --- Patch: _execute_tool ---
    def _patch_execute_tool(self):
        original = self.resident._execute_tool
        monitor = self

        def patched(tool_name, tool_input, stream, topic, sender, message):
            request_id = str(uuid4())
            monitor.events.put(ToolCallPending(
                request_id=request_id,
                tool_name=tool_name,
                tool_input=dict(tool_input) if tool_input else {},
                turn=monitor._current_turn,
                stream=stream, topic=topic,
            ))

            needs_gate = (
                not monitor.auto_approve_tools
                or tool_name in monitor.gated_tools
            )

            if needs_gate:
                decision = monitor.gate.wait_for_decision(request_id, timeout=300)
                if decision.action == 'deny':
                    result = {"content": f"Error: operation not permitted", "is_error": True}
                    monitor.events.put(ToolCallResult(
                        request_id=request_id, tool_name=tool_name,
                        result_content=result["content"], is_error=True,
                    ))
                    return result
                if decision.action == 'mock':
                    result = {"content": decision.mock_result or ""}
                    monitor.events.put(ToolCallResult(
                        request_id=request_id, tool_name=tool_name,
                        result_content=result["content"], is_error=False,
                    ))
                    return result
                if decision.action == 'modify' and decision.modified_input:
                    tool_input = decision.modified_input

            result = original(tool_name, tool_input, stream, topic, sender, message)

            monitor.events.put(ToolCallResult(
                request_id=request_id, tool_name=tool_name,
                result_content=result.get("content", "")[:500],
                is_error=result.get("is_error", False),
            ))
            return result

        self.resident._execute_tool = patched

    # --- Patch: _post_response ---
    def _patch_post_response(self):
        original = self.resident._post_response
        monitor = self

        def patched(original_message, response):
            request_id = str(uuid4())
            stream = monitor._current_stream
            topic = monitor._current_topic

            if not monitor.auto_approve_responses:
                monitor.events.put(ResponseReady(
                    request_id=request_id,
                    stream=stream, topic=topic,
                    content=response,
                ))
                decision = monitor.gate.wait_for_decision(request_id, timeout=300)
                if decision.action == 'deny':
                    monitor.events.put(LogEvent(
                        level="WARNING", source="monitor",
                        message=f"Response suppressed in #{stream}>{topic}",
                    ))
                    return
                if decision.action == 'edit' and decision.edited_content:
                    response = decision.edited_content

            original(original_message, response)

            monitor.events.put(ResponsePosted(
                stream=stream, topic=topic,
                content=response[:200],
            ))

        self.resident._post_response = patched

    # --- Patch: anthropic.messages.create ---
    def _patch_api_create(self):
        original_create = self.resident.anthropic.messages.create
        monitor = self

        def patched(**kwargs):
            system = kwargs.get("system", [])
            messages = kwargs.get("messages", [])
            tools = kwargs.get("tools", [])

            monitor.events.put(ApiRequestSent(
                turn=monitor._current_turn,
                model=kwargs.get("model", ""),
                message_count=len(messages),
                tool_count=len(tools),
                system_block_count=len(system) if isinstance(system, list) else 1,
            ))

            response = original_create(**kwargs)

            usage = response.usage
            input_tokens = getattr(usage, 'input_tokens', 0)
            output_tokens = getattr(usage, 'output_tokens', 0)
            cache_read = getattr(usage, 'cache_read_input_tokens', 0)
            cache_create = getattr(usage, 'cache_creation_input_tokens', 0)

            monitor.events.put(ApiResponseReceived(
                turn=monitor._current_turn,
                stop_reason=response.stop_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read=cache_read,
                cache_create=cache_create,
            ))

            monitor._current_turn += 1
            return response

        self.resident.anthropic.messages.create = patched

    # --- Patch: StateManager writes ---
    def _patch_state_writes(self):
        original_write = self.resident.state.write_file
        original_append = self.resident.state.append_file
        monitor = self

        def patched_write(path, content):
            result = original_write(path, content)
            monitor.events.put(StateFileChanged(
                path=path, action='write',
                content_preview=content[:200] if content else "",
            ))
            return result

        def patched_append(path, content):
            result = original_append(path, content)
            monitor.events.put(StateFileChanged(
                path=path, action='append',
                content_preview=content[:200] if content else "",
            ))
            return result

        self.resident.state.write_file = patched_write
        self.resident.state.append_file = patched_append

    # --- Patch: StateManager.log_message ---
    def _patch_state_log(self):
        original_log = self.resident.state.log_message
        monitor = self

        def patched_log(stream, topic, sender, content, timestamp):
            original_log(stream, topic, sender, content, timestamp)
            # Don't emit for every backfilled message — only if harness is running
            if monitor._response_in_flight or monitor._current_stream:
                monitor.events.put(LogEvent(
                    level="DEBUG", source="state",
                    message=f"log_message #{stream}>{topic} from {sender} ({len(content)} chars)",
                ))

        self.resident.state.log_message = patched_log

    # --- Patch: SessionManager lifecycle ---
    def _patch_sessions(self):
        sm = self.resident.sessions
        original_create = sm.create
        original_remove = sm.remove
        monitor = self

        def patched_create(stream, topic):
            session = original_create(stream, topic)
            monitor.events.put(SessionEvent(
                stream=stream, topic=topic, event_type="created",
            ))
            return session

        def patched_remove(stream, topic):
            original_remove(stream, topic)
            monitor.events.put(SessionEvent(
                stream=stream, topic=topic, event_type="removed",
            ))

        sm.create = patched_create
        sm.remove = patched_remove

    # --- Patch: EngagementJudge ---
    def _patch_engagement(self):
        original = self.resident.judge.should_respond
        monitor = self
        judge = self.resident.judge

        def patched(message, stream):
            result = original(message, stream)
            sender = message.get("sender_full_name", "unknown")
            reason = getattr(judge, 'last_reason', 'unknown')

            monitor.events.put(EngagementDecision(
                stream=stream,
                topic=message.get("subject", message.get("sender_email", "")),
                sender=sender,
                should_respond=result,
                reason=reason,
            ))
            return result

        self.resident.judge.should_respond = patched

    # --- Patch: run() → emit ZulipConnected + reset turn counter per message ---
    def _patch_run(self):
        original_run = self.resident.run
        monitor = self

        # Also patch the zulip register call to capture queue_id
        original_register = self.resident.zulip.register

        def patched_register(**kwargs):
            result = original_register(**kwargs)
            if result.get("result") == "success":
                monitor.events.put(ZulipConnected(
                    queue_id=result.get("queue_id", ""),
                ))
            return result

        self.resident.zulip.register = patched_register

        # Patch handle_message to reset turn counter
        inner_handle = self.resident.handle_message  # already patched above

        def reset_turn_handle(message):
            monitor._current_turn = 0
            return inner_handle(message)

        self.resident.handle_message = reset_turn_handle


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _hex_color(hex_str: str) -> list[int]:
    """Convert '#RRGGBB' or '#RRGGBBAA' to [R, G, B, A]."""
    hex_str = hex_str.lstrip('#')
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    a = int(hex_str[6:8], 16) if len(hex_str) == 8 else 255
    return [r, g, b, a]


# ---------------------------------------------------------------------------
# MonitorApp — DearPyGui dashboard
# ---------------------------------------------------------------------------

class MonitorApp:
    """DearPyGui monitoring dashboard."""

    MAX_LOG_ENTRIES = 2000

    def __init__(self, harness_monitor: HarnessMonitor, state_dir: Path):
        self.monitor = harness_monitor
        self.events = harness_monitor.events
        self.state_dir = state_dir

        # UI state
        self._log_entries: list[str] = []
        self._log_colors: list[list[int]] = []
        self._selected_state_file: Optional[str] = None
        self._api_history: list[dict] = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cache_read = 0
        self._total_messages = 0
        self._total_responses = 0
        self._pending_tool_calls: list[ToolCallPending] = []
        self._pending_responses: list[ResponseReady] = []
        self._tool_edit_buffer: str = ""
        self._response_edit_buffer: str = ""
        self._mock_result_buffer: str = ""
        self._state_edit_buffer: str = ""
        self._state_editing: bool = False
        self._live_entries: list[dict] = []  # conversation flow entries
        self._auto_scroll_log = True
        self._intervention_visible = False
        self._state_tree_built = False

    def setup(self):
        """Create all DearPyGui widgets."""
        dpg.create_context()
        dpg.create_viewport(title="Claude Monitor", width=1600, height=1000)

        # -- Theme --
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 8)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 4)
        dpg.bind_theme(global_theme)

        # -- Primary window --
        with dpg.window(tag="primary", label="Claude Monitor"):
            # Status bar
            with dpg.group(horizontal=True, tag="status_bar"):
                dpg.add_text("Claude Monitor", color=_hex_color("#4FC3F7"))
                dpg.add_text(" | ", color=_hex_color("#555555"))
                dpg.add_text("Disconnected", tag="status_connection",
                             color=_hex_color("#FF5252"))
                dpg.add_text(" | ", color=_hex_color("#555555"))
                dpg.add_text("Msgs: 0", tag="status_messages")
                dpg.add_text(" | ", color=_hex_color("#555555"))
                dpg.add_text("Tokens: 0", tag="status_tokens")
                dpg.add_text(" | ", color=_hex_color("#555555"))
                dpg.add_text("Gates: 0", tag="status_gates")
                dpg.add_text(" | ", color=_hex_color("#555555"))
                dpg.add_text("Mode: Autopilot", tag="status_mode",
                             color=_hex_color("#66BB6A"))

            dpg.add_separator()

            # Tab bar (in a child window so the event log stays visible below)
            with dpg.child_window(border=False, height=-290):
              with dpg.tab_bar(tag="main_tabs"):
                # -- Live tab --
                with dpg.tab(label="Live", tag="tab_live"):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Conversation Flow",
                                     color=_hex_color("#4FC3F7"))
                        dpg.add_text("  ", color=_hex_color("#333333"))
                        dpg.add_text("Responses: 0", tag="live_resp_count",
                                     color=_hex_color("#AAAAAA"))
                    dpg.add_separator()
                    with dpg.child_window(tag="live_scroll", height=-1,
                                          border=True,
                                          horizontal_scrollbar=True):
                        dpg.add_text("Waiting for messages...",
                                     tag="live_placeholder",
                                     color=_hex_color("#777777"))

                # -- State tab --
                with dpg.tab(label="State", tag="tab_state"):
                    with dpg.group(horizontal=True):
                        # File tree
                        with dpg.child_window(tag="state_tree_panel",
                                              width=300, border=True):
                            dpg.add_text("State Files",
                                         color=_hex_color("#4FC3F7"))
                            dpg.add_separator()
                            dpg.add_button(label="Refresh",
                                           callback=self._refresh_state_tree)
                            dpg.add_separator()
                            with dpg.group(tag="state_tree_group"):
                                pass  # populated dynamically

                        # File content
                        with dpg.child_window(tag="state_content_panel",
                                              border=True):
                            dpg.add_text("Select a file",
                                         tag="state_file_header",
                                         color=_hex_color("#4FC3F7"))
                            dpg.add_separator()
                            with dpg.group(horizontal=True, tag="state_buttons"):
                                dpg.add_button(label="Edit",
                                               tag="state_edit_btn",
                                               callback=self._toggle_state_edit,
                                               show=False)
                                dpg.add_button(label="Save",
                                               tag="state_save_btn",
                                               callback=self._save_state_file,
                                               show=False)
                                dpg.add_button(label="Cancel",
                                               tag="state_cancel_btn",
                                               callback=self._cancel_state_edit,
                                               show=False)
                            dpg.add_input_text(
                                tag="state_content_view",
                                multiline=True, readonly=True,
                                height=-1, width=-1,
                                default_value="",
                                tab_input=True,
                            )

                # -- API tab --
                with dpg.tab(label="API", tag="tab_api"):
                    dpg.add_text("API Call History",
                                 color=_hex_color("#4FC3F7"))
                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_text("Total input:", color=_hex_color("#AAAAAA"))
                        dpg.add_text("0", tag="api_total_input")
                        dpg.add_text("  output:", color=_hex_color("#AAAAAA"))
                        dpg.add_text("0", tag="api_total_output")
                        dpg.add_text("  cached:", color=_hex_color("#AAAAAA"))
                        dpg.add_text("0", tag="api_total_cached")
                    dpg.add_separator()
                    with dpg.table(tag="api_table", header_row=True,
                                   borders_innerH=True, borders_outerH=True,
                                   borders_innerV=True, borders_outerV=True,
                                   resizable=True, scrollY=True, height=-1):
                        dpg.add_table_column(label="Time", width_fixed=True,
                                             init_width_or_weight=80)
                        dpg.add_table_column(label="Turn", width_fixed=True,
                                             init_width_or_weight=40)
                        dpg.add_table_column(label="Input Tok",
                                             width_fixed=True,
                                             init_width_or_weight=80)
                        dpg.add_table_column(label="Output Tok",
                                             width_fixed=True,
                                             init_width_or_weight=80)
                        dpg.add_table_column(label="Cache Read",
                                             width_fixed=True,
                                             init_width_or_weight=80)
                        dpg.add_table_column(label="Cache Create",
                                             width_fixed=True,
                                             init_width_or_weight=90)
                        dpg.add_table_column(label="Stop Reason")

                # -- Sessions tab --
                with dpg.tab(label="Sessions", tag="tab_sessions"):
                    dpg.add_text("Active Sessions",
                                 color=_hex_color("#4FC3F7"))
                    dpg.add_separator()
                    dpg.add_button(label="Refresh",
                                   callback=self._refresh_sessions)
                    dpg.add_separator()
                    with dpg.table(tag="sessions_table", header_row=True,
                                   borders_innerH=True, borders_outerH=True,
                                   borders_innerV=True, borders_outerV=True,
                                   resizable=True):
                        dpg.add_table_column(label="Stream")
                        dpg.add_table_column(label="Topic")
                        dpg.add_table_column(label="Messages",
                                             width_fixed=True,
                                             init_width_or_weight=70)
                        dpg.add_table_column(label="Age (s)",
                                             width_fixed=True,
                                             init_width_or_weight=70)

                # -- Config tab --
                with dpg.tab(label="Config", tag="tab_config"):
                    dpg.add_text("Intervention Settings",
                                 color=_hex_color("#4FC3F7"))
                    dpg.add_separator()

                    dpg.add_checkbox(label="Auto-approve all tools",
                                     tag="cfg_auto_tools",
                                     default_value=True,
                                     callback=self._on_auto_tools_changed)
                    dpg.add_checkbox(label="Auto-approve responses",
                                     tag="cfg_auto_responses",
                                     default_value=True,
                                     callback=self._on_auto_responses_changed)

                    dpg.add_separator()
                    dpg.add_text("Per-tool gating (active when auto-approve is ON):",
                                 color=_hex_color("#AAAAAA"))
                    dpg.add_text("These tools still require approval even in autopilot:",
                                 color=_hex_color("#777777"))
                    dpg.add_separator()

                    all_tools = [
                        "read_state_file", "write_state_file",
                        "list_state_files", "search_messages",
                        "search_zulip_history", "send_sysadmin_message",
                        "get_person_notes", "run_sandbox",
                        "get_sandbox_file", "upload_sandbox_file",
                        "fetch_url", "web_search",
                        "run_background", "edit_harness",
                    ]
                    for tool in all_tools:
                        default = tool in self.monitor.gated_tools
                        dpg.add_checkbox(
                            label=tool,
                            tag=f"cfg_gate_{tool}",
                            default_value=default,
                            callback=self._on_tool_gate_changed,
                            user_data=tool,
                        )

                    dpg.add_separator()
                    dpg.add_text("Special Modes",
                                 color=_hex_color("#4FC3F7"))
                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Trigger Reflect",
                                       callback=self._trigger_reflect)
                        dpg.add_button(label="Trigger Ambient Check",
                                       callback=self._trigger_ambient)

            # -- Event log (always visible at bottom) --
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_text("Event Log", color=_hex_color("#4FC3F7"))
                dpg.add_checkbox(label="Auto-scroll",
                                 tag="log_autoscroll",
                                 default_value=True,
                                 callback=lambda s, v: setattr(
                                     self, '_auto_scroll_log', v))
                dpg.add_button(label="Clear",
                               callback=self._clear_log)
            with dpg.child_window(tag="log_scroll", height=250, border=True):
                pass  # log entries added dynamically

        # -- Intervention modal (tool calls) --
        with dpg.window(tag="tool_gate_modal", label="Tool Call — Approval Required",
                        modal=True, show=False, no_close=True,
                        width=700, height=500, pos=[450, 250]):
            dpg.add_text("", tag="gate_tool_header",
                         color=_hex_color("#FFB74D"))
            dpg.add_text("", tag="gate_tool_context",
                         color=_hex_color("#AAAAAA"))
            dpg.add_separator()
            dpg.add_text("Input:", color=_hex_color("#4FC3F7"))
            dpg.add_input_text(tag="gate_tool_input", multiline=True,
                               height=200, width=-1, readonly=True)
            dpg.add_separator()
            dpg.add_text("Mock result (for Mock action):",
                         color=_hex_color("#AAAAAA"))
            dpg.add_input_text(tag="gate_mock_input", multiline=True,
                               height=60, width=-1)
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Approve",
                               callback=lambda: self._resolve_tool_gate('approve'))
                dpg.add_button(label="Deny",
                               callback=lambda: self._resolve_tool_gate('deny'))
                dpg.add_button(label="Mock Result",
                               callback=lambda: self._resolve_tool_gate('mock'))

        # -- Intervention modal (responses) --
        with dpg.window(tag="response_gate_modal",
                        label="Response — Review Required",
                        modal=True, show=False, no_close=True,
                        width=700, height=500, pos=[450, 250]):
            dpg.add_text("", tag="gate_resp_header",
                         color=_hex_color("#CE93D8"))
            dpg.add_separator()
            dpg.add_text("Response content:", color=_hex_color("#4FC3F7"))
            dpg.add_input_text(tag="gate_resp_content", multiline=True,
                               height=300, width=-1)
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Send As-Is",
                               callback=lambda: self._resolve_response_gate('approve'))
                dpg.add_button(label="Send Edited",
                               callback=lambda: self._resolve_response_gate('edit'))
                dpg.add_button(label="Suppress",
                               callback=lambda: self._resolve_response_gate('deny'))

        dpg.set_primary_window("primary", True)
        dpg.setup_dearpygui()
        dpg.show_viewport()

    def run(self):
        """Main render loop — drains events and repaints."""
        while dpg.is_dearpygui_running():
            self._drain_events()
            self._update_status_bar()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def _drain_events(self):
        """Process all pending events from the background thread."""
        count = 0
        while count < 100:  # cap per frame to avoid UI stall
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
            count += 1

    def _handle_event(self, event):
        """Route an event to the appropriate handler."""
        if isinstance(event, MessageReceived):
            self._total_messages += 1
            self._add_log(
                f"MSG  #{event.stream}>{event.topic} | "
                f"{event.sender}: {event.content[:80]}",
                _hex_color("#64B5F6"),  # blue
            )
            self._add_live_entry("message", {
                "sender": event.sender,
                "content": event.content,
                "stream": event.stream,
                "topic": event.topic,
            })

        elif isinstance(event, EngagementDecision):
            color = _hex_color("#66BB6A") if event.should_respond \
                else _hex_color("#777777")
            verb = "RESPOND" if event.should_respond else "SKIP"
            self._add_log(
                f"JUDGE {verb} ({event.reason}) "
                f"#{event.stream}>{event.topic}",
                color,
            )

        elif isinstance(event, ToolCallPending):
            self._add_log(
                f"TOOL  {event.tool_name} "
                f"(turn {event.turn}, #{event.stream}>{event.topic})",
                _hex_color("#FFB74D"),  # orange
            )
            self._add_live_entry("tool_call", {
                "name": event.tool_name,
                "input": event.tool_input,
                "request_id": event.request_id,
            })
            # If this tool is gated, show the modal
            if (not self.monitor.auto_approve_tools
                    or event.tool_name in self.monitor.gated_tools):
                self._pending_tool_calls.append(event)
                self._show_tool_gate_modal()

        elif isinstance(event, ToolCallResult):
            color = _hex_color("#EF5350") if event.is_error \
                else _hex_color("#66BB6A")
            status = "ERR" if event.is_error else "OK"
            self._add_log(
                f"RESULT {event.tool_name} [{status}] "
                f"{event.result_content[:80]}",
                color,
            )

        elif isinstance(event, ApiRequestSent):
            self._add_log(
                f"API   turn {event.turn} sent "
                f"({event.message_count} msgs, {event.tool_count} tools)",
                _hex_color("#FFB74D"),
            )

        elif isinstance(event, ApiResponseReceived):
            self._total_input_tokens += event.input_tokens
            self._total_output_tokens += event.output_tokens
            self._total_cache_read += event.cache_read
            self._add_log(
                f"API   turn {event.turn} recv "
                f"[{event.stop_reason}] "
                f"in:{event.input_tokens} out:{event.output_tokens} "
                f"cache:{event.cache_read}r/{event.cache_create}c",
                _hex_color("#4FC3F7"),
            )
            # Add to API table
            self._add_api_row(event)

        elif isinstance(event, ResponseReady):
            self._pending_responses.append(event)
            self._show_response_gate_modal()

        elif isinstance(event, ResponsePosted):
            self._total_responses += 1
            self._add_log(
                f"POST  #{event.stream}>{event.topic} "
                f"({len(event.content)} chars)",
                _hex_color("#CE93D8"),  # purple
            )
            self._add_live_entry("response", {
                "stream": event.stream,
                "topic": event.topic,
                "content": event.content,
            })
            dpg.set_value("live_resp_count",
                          f"Responses: {self._total_responses}")

        elif isinstance(event, StateFileChanged):
            self._add_log(
                f"STATE {event.action} {event.path} "
                f"({len(event.content_preview)} chars preview)",
                _hex_color("#FFF176"),  # yellow
            )
            # Auto-refresh if viewing this file
            if self._selected_state_file == event.path:
                self._load_state_file(event.path)

        elif isinstance(event, SessionEvent):
            self._add_log(
                f"SESSION {event.event_type} "
                f"#{event.stream}>{event.topic}",
                _hex_color("#4DB6AC"),
            )

        elif isinstance(event, ZulipConnected):
            self._add_log(
                f"ZULIP connected (queue: {event.queue_id})",
                _hex_color("#66BB6A"),
            )
            dpg.set_value("status_connection", "Connected")
            dpg.configure_item("status_connection",
                               color=_hex_color("#66BB6A"))

        elif isinstance(event, ErrorEvent):
            self._add_log(
                f"ERROR [{event.context}] {event.message}",
                _hex_color("#EF5350"),
            )

        elif isinstance(event, LogEvent):
            color_map = {
                "DEBUG": _hex_color("#777777"),
                "INFO": _hex_color("#AAAAAA"),
                "WARNING": _hex_color("#FFB74D"),
                "ERROR": _hex_color("#EF5350"),
            }
            color = color_map.get(event.level, _hex_color("#AAAAAA"))
            self._add_log(
                f"[{event.level}] {event.source}: {event.message}",
                color,
            )

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _add_log(self, text: str, color: list[int]):
        """Add a line to the event log."""
        ts = datetime.now().strftime("%H:%M:%S")
        full_text = f"[{ts}] {text}"

        # Add to DearPyGui
        tag = f"log_{len(self._log_entries)}"
        dpg.add_text(full_text, parent="log_scroll", color=color, tag=tag)

        self._log_entries.append(full_text)
        self._log_colors.append(color)

        # Trim old entries
        if len(self._log_entries) > self.MAX_LOG_ENTRIES:
            old_tag = f"log_{len(self._log_entries) - self.MAX_LOG_ENTRIES - 1}"
            if dpg.does_item_exist(old_tag):
                dpg.delete_item(old_tag)

        # Auto-scroll
        if self._auto_scroll_log:
            dpg.set_y_scroll("log_scroll", dpg.get_y_scroll_max("log_scroll") + 100)

    def _add_live_entry(self, entry_type: str, data: dict):
        """Add an entry to the Live conversation view."""
        self._live_entries.append({"type": entry_type, "data": data,
                                   "time": datetime.now()})

        # Remove placeholder if present
        if dpg.does_item_exist("live_placeholder"):
            dpg.delete_item("live_placeholder")

        tag = f"live_{len(self._live_entries)}"
        ts = datetime.now().strftime("%H:%M:%S")

        if entry_type == "message":
            color = _hex_color("#64B5F6")
            text = f"[{ts}] {data['sender']}: {data['content'][:120]}"
        elif entry_type == "tool_call":
            color = _hex_color("#FFB74D")
            inp_preview = json.dumps(data['input'])[:100]
            text = f"[{ts}] TOOL {data['name']}({inp_preview})"
        elif entry_type == "response":
            color = _hex_color("#CE93D8")
            text = f"[{ts}] CLAUDE: {data['content'][:120]}"
        else:
            color = _hex_color("#AAAAAA")
            text = f"[{ts}] {entry_type}: {str(data)[:100]}"

        dpg.add_text(text, parent="live_scroll", color=color, tag=tag,
                     wrap=0)

    def _update_status_bar(self):
        """Update the status bar values."""
        dpg.set_value("status_messages", f"Msgs: {self._total_messages}")
        dpg.set_value("status_tokens",
                      f"Tokens: {self._total_input_tokens + self._total_output_tokens:,}")

        gates = self.monitor.gate.pending_count
        dpg.set_value("status_gates", f"Gates: {gates}")
        if gates > 0:
            dpg.configure_item("status_gates", color=_hex_color("#FF5252"))
        else:
            dpg.configure_item("status_gates", color=_hex_color("#AAAAAA"))

        if self.monitor.auto_approve_tools and self.monitor.auto_approve_responses:
            dpg.set_value("status_mode", "Mode: Autopilot")
            dpg.configure_item("status_mode", color=_hex_color("#66BB6A"))
        else:
            dpg.set_value("status_mode", "Mode: Gated")
            dpg.configure_item("status_mode", color=_hex_color("#FFB74D"))

    def _add_api_row(self, event: ApiResponseReceived):
        """Add a row to the API history table."""
        ts = datetime.now().strftime("%H:%M:%S")
        with dpg.table_row(parent="api_table"):
            dpg.add_text(ts)
            dpg.add_text(str(event.turn))
            dpg.add_text(f"{event.input_tokens:,}")
            dpg.add_text(f"{event.output_tokens:,}")
            dpg.add_text(f"{event.cache_read:,}")
            dpg.add_text(f"{event.cache_create:,}")
            dpg.add_text(event.stop_reason)

        # Update totals
        dpg.set_value("api_total_input", f"{self._total_input_tokens:,}")
        dpg.set_value("api_total_output", f"{self._total_output_tokens:,}")
        dpg.set_value("api_total_cached", f"{self._total_cache_read:,}")

    # ------------------------------------------------------------------
    # State browser
    # ------------------------------------------------------------------

    def _refresh_state_tree(self):
        """Rebuild the state file tree."""
        # Clear existing tree
        for child in dpg.get_item_children("state_tree_group", 1) or []:
            dpg.delete_item(child)

        self._build_state_tree(self.state_dir, "state_tree_group", "")
        self._state_tree_built = True

    def _build_state_tree(self, path: Path, parent: str, prefix: str):
        """Recursively build tree nodes for a directory."""
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except PermissionError:
            return

        for entry in entries:
            rel = str(entry.relative_to(self.state_dir))
            if entry.name.startswith('.') or entry.name == '__pycache__':
                continue

            if entry.is_dir():
                with dpg.tree_node(label=f"  {entry.name}/", parent=parent,
                                   default_open=False):
                    self._build_state_tree(entry, dpg.last_item(), rel)
            else:
                size = entry.stat().st_size
                if size > 1024 * 1024:
                    size_str = f"{size / (1024*1024):.1f}MB"
                elif size > 1024:
                    size_str = f"{size / 1024:.1f}KB"
                else:
                    size_str = f"{size}B"
                label = f"{entry.name}  ({size_str})"
                dpg.add_selectable(
                    label=label, parent=parent,
                    callback=self._on_state_file_selected,
                    user_data=rel,
                )

    def _on_state_file_selected(self, sender, value, user_data):
        """Handle clicking a state file in the tree."""
        self._selected_state_file = user_data
        self._state_editing = False
        self._load_state_file(user_data)
        dpg.configure_item("state_edit_btn", show=True)
        dpg.configure_item("state_save_btn", show=False)
        dpg.configure_item("state_cancel_btn", show=False)
        dpg.configure_item("state_content_view", readonly=True)

    def _load_state_file(self, rel_path: str):
        """Load a state file into the content viewer."""
        full_path = self.state_dir / rel_path
        dpg.set_value("state_file_header", rel_path)
        try:
            content = full_path.read_text()
            dpg.set_value("state_content_view", content)
            self._state_edit_buffer = content
        except UnicodeDecodeError:
            dpg.set_value("state_content_view", f"[Binary file: {full_path.stat().st_size} bytes]")
        except FileNotFoundError:
            dpg.set_value("state_content_view", "[File not found]")

    def _toggle_state_edit(self):
        """Enter edit mode for the selected state file."""
        self._state_editing = True
        dpg.configure_item("state_edit_btn", show=False)
        dpg.configure_item("state_save_btn", show=True)
        dpg.configure_item("state_cancel_btn", show=True)
        dpg.configure_item("state_content_view", readonly=False)

    def _save_state_file(self):
        """Save the edited state file."""
        if not self._selected_state_file:
            return
        content = dpg.get_value("state_content_view")
        full_path = self.state_dir / self._selected_state_file
        full_path.write_text(content)
        self._state_editing = False
        dpg.configure_item("state_edit_btn", show=True)
        dpg.configure_item("state_save_btn", show=False)
        dpg.configure_item("state_cancel_btn", show=False)
        dpg.configure_item("state_content_view", readonly=True)
        self._add_log(
            f"STATE manual edit: {self._selected_state_file}",
            _hex_color("#FFF176"),
        )

    def _cancel_state_edit(self):
        """Cancel editing and reload the file."""
        self._state_editing = False
        dpg.configure_item("state_edit_btn", show=True)
        dpg.configure_item("state_save_btn", show=False)
        dpg.configure_item("state_cancel_btn", show=False)
        dpg.configure_item("state_content_view", readonly=True)
        if self._selected_state_file:
            self._load_state_file(self._selected_state_file)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _refresh_sessions(self):
        """Refresh the sessions table."""
        # Clear existing rows
        for child in dpg.get_item_children("sessions_table", 1) or []:
            dpg.delete_item(child)

        sessions = self.monitor.resident.sessions._sessions
        for (stream, topic), session in sessions.items():
            with dpg.table_row(parent="sessions_table"):
                dpg.add_text(stream)
                dpg.add_text(topic)
                dpg.add_text(str(session.message_count))
                dpg.add_text(f"{session.age_seconds:.0f}")

    # ------------------------------------------------------------------
    # Intervention modals
    # ------------------------------------------------------------------

    def _show_tool_gate_modal(self):
        """Show the tool call approval modal."""
        if not self._pending_tool_calls:
            return
        pending = self._pending_tool_calls[0]

        dpg.set_value("gate_tool_header",
                      f"Tool: {pending.tool_name}")
        dpg.set_value("gate_tool_context",
                      f"Turn {pending.turn} in #{pending.stream}>{pending.topic}")
        dpg.set_value("gate_tool_input",
                      json.dumps(pending.tool_input, indent=2))
        dpg.set_value("gate_mock_input", "")
        dpg.configure_item("tool_gate_modal", show=True)

    def _resolve_tool_gate(self, action: str):
        """Resolve the pending tool call gate."""
        if not self._pending_tool_calls:
            return
        pending = self._pending_tool_calls.pop(0)

        if action == 'mock':
            mock_text = dpg.get_value("gate_mock_input") or ""
            decision = Decision(action='mock', mock_result=mock_text)
        else:
            decision = Decision(action=action)

        self.monitor.gate.resolve(pending.request_id, decision)
        dpg.configure_item("tool_gate_modal", show=False)

        self._add_log(
            f"GATE  {pending.tool_name} → {action.upper()}",
            _hex_color("#FFB74D"),
        )

        # Show next pending if any
        if self._pending_tool_calls:
            self._show_tool_gate_modal()

    def _show_response_gate_modal(self):
        """Show the response review modal."""
        if not self._pending_responses:
            return
        pending = self._pending_responses[0]

        dpg.set_value("gate_resp_header",
                      f"Response for #{pending.stream}>{pending.topic}")
        dpg.set_value("gate_resp_content", pending.content)
        dpg.configure_item("response_gate_modal", show=True)

    def _resolve_response_gate(self, action: str):
        """Resolve the pending response gate."""
        if not self._pending_responses:
            return
        pending = self._pending_responses.pop(0)

        if action == 'edit':
            edited = dpg.get_value("gate_resp_content")
            decision = Decision(action='edit', edited_content=edited)
        else:
            decision = Decision(action=action)

        self.monitor.gate.resolve(pending.request_id, decision)
        dpg.configure_item("response_gate_modal", show=False)

        self._add_log(
            f"GATE  response → {action.upper()}",
            _hex_color("#CE93D8"),
        )

        if self._pending_responses:
            self._show_response_gate_modal()

    # ------------------------------------------------------------------
    # Config callbacks
    # ------------------------------------------------------------------

    def _on_auto_tools_changed(self, sender, value):
        self.monitor.auto_approve_tools = value

    def _on_auto_responses_changed(self, sender, value):
        self.monitor.auto_approve_responses = value

    def _on_tool_gate_changed(self, sender, value, user_data):
        tool_name = user_data
        if value:
            self.monitor.gated_tools.add(tool_name)
        else:
            self.monitor.gated_tools.discard(tool_name)

    # ------------------------------------------------------------------
    # Special modes
    # ------------------------------------------------------------------

    def _trigger_reflect(self):
        """Trigger a reflection cycle in a background thread."""
        def _run():
            try:
                self.monitor.events.put(LogEvent(
                    level="INFO", source="monitor",
                    message="Starting reflection cycle...",
                ))
                self.monitor.resident.reflect()
                self.monitor.events.put(LogEvent(
                    level="INFO", source="monitor",
                    message="Reflection cycle complete.",
                ))
            except Exception as e:
                self.monitor.events.put(ErrorEvent(
                    context="reflect", message=str(e),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _trigger_ambient(self):
        """Trigger an ambient check in a background thread."""
        def _run():
            try:
                self.monitor.events.put(LogEvent(
                    level="INFO", source="monitor",
                    message="Starting ambient check...",
                ))
                self.monitor.resident.check_ambient()
                self.monitor.events.put(LogEvent(
                    level="INFO", source="monitor",
                    message="Ambient check complete.",
                ))
            except Exception as e:
                self.monitor.events.put(ErrorEvent(
                    context="ambient", message=str(e),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _clear_log(self):
        """Clear the event log."""
        for child in dpg.get_item_children("log_scroll", 1) or []:
            dpg.delete_item(child)
        self._log_entries.clear()
        self._log_colors.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Claude Monitor Dashboard")
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
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )

    # Create event queue (shared between harness and UI)
    event_queue = queue.Queue(maxsize=10000)

    # Capture harness logs into the event queue
    log_handler = QueueLogHandler(event_queue)
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("claude_resident").addHandler(log_handler)
    logging.getLogger("session_manager").addHandler(log_handler)

    # Initialize shared clients (persist across harness reloads)
    import zulip as zulip_mod
    import anthropic as anthropic_mod

    zulip_kwargs = {}
    if args.zuliprc:
        zulip_kwargs["config_file"] = args.zuliprc
    zulip_client = zulip_mod.Client(**zulip_kwargs)
    anthropic_client = anthropic_mod.Anthropic()
    bot_profile = zulip_client.get_profile()
    bot_name = bot_profile.get("full_name", "Claude")

    logging.getLogger("monitor").info(f"Bot name: {bot_name}")

    # Harness creation helper — used for initial boot and after self-edit reloads.
    # Always pulls classes from the (possibly reloaded) claude_resident module.
    def create_harness():
        CR = claude_resident.ClaudeResident
        SM = claude_resident.StateManager
        EJ = claude_resident.EngagementJudge
        st = SM(args.state_dir)
        jg = EJ(bot_name, args.standing_streams,
               anthropic_client=anthropic_client, state_manager=st)
        res = CR(
            zulip_client=zulip_client,
            anthropic_client=anthropic_client,
            state=st, judge=jg, model=args.model,
        )
        mon = HarnessMonitor(res, event_queue)
        return res, mon

    resident, harness_monitor = create_harness()

    event_queue.put(LogEvent(
        level="INFO", source="monitor",
        message=f"Harness initialized. Bot: {bot_name}, Model: {args.model}",
    ))

    # Start DearPyGui dashboard on the main thread (must setup before harness thread)
    app = MonitorApp(harness_monitor, args.state_dir)
    app.setup()
    app._refresh_state_tree()

    # ------------------------------------------------------------------
    # Supervisor loop — runs in background thread, handles self-edit
    # reloads, crash-after-edit rollback, and crash loop detection.
    # Mirrors the supervisor in claude_resident.main() but in-process:
    # instead of relaunching a subprocess, we importlib.reload the module,
    # recreate the ClaudeResident, and re-apply monkey patches.
    # ------------------------------------------------------------------
    def supervisor_loop():
        nonlocal resident, harness_monitor

        CRASH_WINDOW = 60
        MAX_CRASHES = 3
        crash_times: list[float] = []
        logger = logging.getLogger("supervisor")

        while True:
            try:
                event_queue.put(LogEvent(
                    level="INFO", source="supervisor",
                    message="Starting harness event loop...",
                ))
                resident.run()
                # Clean return (e.g. KeyboardInterrupt caught internally)
                event_queue.put(LogEvent(
                    level="INFO", source="supervisor",
                    message="Harness event loop ended cleanly",
                ))
                break

            except SystemExit as e:
                if e.code == 42:
                    # ---- Self-edit restart ----
                    logger.info("Supervisor: self-edit restart (exit 42)")
                    event_queue.put(LogEvent(
                        level="WARNING", source="supervisor",
                        message="Self-edit detected — reloading harness...",
                    ))
                    crash_times.clear()

                    try:
                        importlib.reload(claude_resident)
                        resident, harness_monitor = create_harness()
                        app.monitor = harness_monitor
                        event_queue.put(LogEvent(
                            level="INFO", source="supervisor",
                            message="Harness reloaded successfully, restarting...",
                        ))
                        continue
                    except Exception as reload_err:
                        logger.error(f"Supervisor: reload failed: {reload_err}",
                                     exc_info=True)
                        event_queue.put(ErrorEvent(
                            context="supervisor",
                            message=f"Reload failed after self-edit: {reload_err}",
                        ))
                        # Rollback the bad edit
                        if claude_resident._last_commit_is_self_edit():
                            claude_resident._rollback_last_commit(logger)
                            claude_resident._notify_claude(args.state_dir,
                                f"Self-edit caused reload failure ({reload_err}) "
                                f"and was auto-reverted by the supervisor.")
                            event_queue.put(LogEvent(
                                level="WARNING", source="supervisor",
                                message="Self-edit rolled back, reloading original...",
                            ))
                        # Try to recover with the reverted code
                        try:
                            importlib.reload(claude_resident)
                            resident, harness_monitor = create_harness()
                            app.monitor = harness_monitor
                            continue
                        except Exception as e2:
                            event_queue.put(ErrorEvent(
                                context="supervisor",
                                message=f"Cannot recover after rollback: {e2}. "
                                        f"Harness stopped.",
                            ))
                            break
                else:
                    # Non-42 SystemExit — unexpected
                    event_queue.put(ErrorEvent(
                        context="supervisor",
                        message=f"Harness exited with code {e.code}",
                    ))
                    break

            except Exception as e:
                # ---- Crash ----
                logger.error(f"Supervisor: harness crashed: {e}", exc_info=True)
                event_queue.put(ErrorEvent(
                    context="harness", message=f"Crash: {e}",
                ))

                # Check if a self-edit caused this crash
                if claude_resident._last_commit_is_self_edit():
                    logger.warning(
                        "Supervisor: crash after self-edit — rolling back")
                    claude_resident._rollback_last_commit(logger)
                    claude_resident._notify_claude(args.state_dir,
                        f"Self-edit caused a runtime crash ({e}) "
                        f"and was auto-reverted by the supervisor.")
                    event_queue.put(LogEvent(
                        level="WARNING", source="supervisor",
                        message="Self-edit rolled back after crash, restarting...",
                    ))
                    crash_times.clear()
                    try:
                        importlib.reload(claude_resident)
                        resident, harness_monitor = create_harness()
                        app.monitor = harness_monitor
                        continue
                    except Exception as e2:
                        event_queue.put(ErrorEvent(
                            context="supervisor",
                            message=f"Cannot recover after rollback: {e2}. "
                                    f"Harness stopped.",
                        ))
                        break

                # Crash loop detection
                now = time.time()
                crash_times = [t for t in crash_times
                               if now - t < CRASH_WINDOW]
                crash_times.append(now)
                if len(crash_times) >= MAX_CRASHES:
                    event_queue.put(ErrorEvent(
                        context="supervisor",
                        message=f"Crash loop ({MAX_CRASHES}x in "
                                f"{CRASH_WINDOW}s). Harness stopped.",
                    ))
                    claude_resident._notify_claude(args.state_dir,
                        f"Event loop crash-looped "
                        f"({MAX_CRASHES}x in {CRASH_WINDOW}s). "
                        f"Sysadmin intervention needed.")
                    break

                event_queue.put(LogEvent(
                    level="WARNING", source="supervisor",
                    message=f"Restarting after crash "
                            f"({len(crash_times)}/{MAX_CRASHES} in window)",
                ))
                time.sleep(3)

    # Start supervisor AFTER UI is fully initialized
    supervisor_thread = threading.Thread(target=supervisor_loop, daemon=True,
                                         name="supervisor")
    supervisor_thread.start()

    app.run()


if __name__ == "__main__":
    main()
