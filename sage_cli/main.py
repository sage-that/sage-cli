#!/usr/bin/env python3
"""
Sage CLI — Interactive Thought Companion

A thin terminal client that connects to the Sage backend API.
All agent logic runs server-side; CLI handles only rendering and input.

Usage:
    sage                  # Standard mode
    sage --deep           # Deep analysis mode
    sage --mobile         # Concise responses (default)
    sage --debug          # Show investigation queries in detail
    sage --local          # Connect to localhost (no auth)
    sage login            # Authenticate via browser (Google OAuth)
    sage logout           # Remove cached credentials

Environment:
    SAGE_API_URL          Backend API base URL (default: https://api.dev.sagethat.com)
    SAGE_COGNITO_CLIENT_ID Cognito User Pool Client ID (for login)
    SAGE_COGNITO_DOMAIN   Cognito domain (for login)
"""

import asyncio
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from .auth import get_auth_header
from .auth import login as auth_login
from .auth import logout as auth_logout
from .client import SageBackend, SageBackendError

# ═══════════════════════════════════════════════════════════════════════════
# Rendering Pipeline
# ═══════════════════════════════════════════════════════════════════════════

PATTERN_ICONS = {
    "sunrise": "🌅",
    "coffee": "☕",
    "hourglass": "⏳",
    "heart": "❤️",
    "sparkles": "✨",
    "clouds": "☁️",
    "sun": "☀️",
    "umbrella": "☂️",
    "trending-up": "📈",
    "sprout": "🌱",
    "seedling": "🌾",
    "mountain": "⛰️",
    "butterfly": "🦋",
    "compass": "🧭",
    "zap": "⚡",
    "battery": "🔋",
    "flame": "🔥",
    "feather": "🪶",
    "wind": "💨",
    "eye": "👁️",
    "lightbulb": "💡",
    "brain": "🧠",
    "key": "🔑",
    "target": "🎯",
    "anchor": "⚓",
    "bridge": "🌉",
    "shield": "🛡️",
    "waves": "🌊",
    "moon": "🌙",
}

EXIT_MESSAGE = "\n[bold green]👋 Take care![/bold green]"


class EventDispatcher:
    def __init__(self):
        self.handlers: Dict[str, List[Callable]] = {}

    def on(self, event_type: str, handler: Callable) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def off(self, event_type: str, handler: Callable) -> None:
        if event_type in self.handlers:
            self.handlers[event_type].remove(handler)

    async def emit(self, event: dict) -> None:
        for handler in self.handlers.get(event.get("type", ""), []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                pass


@dataclass
class TextSegment:
    content: str = ""


@dataclass
class InvestigationSegment:
    id: str
    title: str
    subtitle: str = ""
    debug_mode: bool = False
    queries: List[str] = field(default_factory=list)
    tool_calls: int = 0
    max_tool_calls: int = 5
    active: bool = True
    notes_count: int = 0
    sources_count: int = 0
    _start_time: float = field(default_factory=lambda: datetime.now().timestamp())
    DOTS = ["   ", ".  ", ".. ", "..."]

    def add_query(self, query: str, tool_calls: int, max_tool_calls: int):
        self.queries.append(query)
        self.tool_calls = tool_calls
        self.max_tool_calls = max_tool_calls

    def complete(self, notes_count=0, sources_count=0, total_queries_run=0):
        self.active = False
        self.notes_count = notes_count
        self.sources_count = sources_count
        if total_queries_run:
            self.tool_calls = total_queries_run

    def _summary(self) -> str:
        parts = []
        if self.notes_count:
            parts.append(f"{self.notes_count} notes")
        if self.sources_count:
            parts.append(f"{self.sources_count} sources")
        return f"Found {', '.join(parts)}" if parts else ""

    def __rich_console__(self, console, options):
        color = "yellow" if self.active else "green"
        header = [f"[bold]{self.title}[/bold]"]
        if self.debug_mode and self.subtitle:
            header.append(f"[cyan]({self.subtitle})[/cyan]")
        checks = " ".join([f"[{color}]✓[/{color}]"] * len(self.queries))
        if self.active:
            elapsed = datetime.now().timestamp() - self._start_time
            dots = self.DOTS[int(elapsed * 0.5) % len(self.DOTS)]
            progress = f"  {checks} [dim]round {self.tool_calls}/{self.max_tool_calls}[/dim] [dim]{dots}[/dim]"
        else:
            progress = f"  {checks} [dim]{self.tool_calls} rounds[/dim]"
        header.extend(["", progress])
        parts = [Text.from_markup("\n".join(header))]
        if self.debug_mode and self.queries:
            table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
            table.add_column("status", width=1, no_wrap=True)
            table.add_column("query", ratio=1, overflow="ellipsis", no_wrap=True)
            for i, q in enumerate(self.queries, 1):
                icon_color = "yellow" if self.active else "green"
                table.add_row(f"[{icon_color}]✓[/{icon_color}]", f"[dim]{i}.[/dim] {q}")
            parts.append(table)
        summary = self._summary()
        if summary:
            parts.append(Text.from_markup(f"\n  [bold green]{summary}[/bold green]"))
        yield Panel(Group(*parts), border_style=color, padding=(0, 1), expand=True)


@dataclass
class ReflectionSegment:
    content: str = ""
    reflection_type: str = "question"
    TYPE_ICONS = {"question": "❓", "reframe": "🔄", "practice": "🧘"}

    def __rich_console__(self, console, options):
        icon = self.TYPE_ICONS.get(self.reflection_type, "✨")
        yield Panel(
            Text.from_markup(f"{icon}  {self.content}"),
            title=f"[bold]Take With You ({self.reflection_type})[/bold]",
            border_style="green",
            padding=(0, 1),
            expand=True,
        )


@dataclass
class MemorySegment:
    content: str = ""
    memory_type: str = "insight"
    TYPE_ICONS = {"commitment": "🤝", "insight": "💡", "follow_up": "📌"}

    def __rich_console__(self, console, options):
        icon = self.TYPE_ICONS.get(self.memory_type, "📝")
        yield Panel(
            Text.from_markup(f"{icon}  {self.content}"),
            title=f"[bold]Remembered ({self.memory_type})[/bold]",
            border_style="magenta",
            padding=(0, 1),
            expand=True,
        )


@dataclass
class DirectionSegment:
    choices: List[str] = field(default_factory=list)
    selected_index: Optional[int] = None
    skipped: bool = False
    user_text: Optional[str] = None

    def __rich_console__(self, console, options):
        lines = []
        if self.skipped:
            lines.append("  → [dim]Skipped[/dim]")
        elif self.user_text:
            lines.append(f"  → [cyan]{self.user_text}[/cyan]")
        else:
            for i, choice in enumerate(self.choices):
                prefix = "  →" if i == self.selected_index else "   "
                color = "cyan" if i == self.selected_index else "dim"
                lines.append(f"{prefix} [{color}]{choice}[/{color}]")
        yield Panel(
            Text.from_markup("\n".join(lines)),
            title="Direction",
            border_style="yellow",
            padding=(0, 1),
            expand=True,
        )


@dataclass
class ContentStream:
    segments: list = field(default_factory=list)
    _investigations: Dict[str, InvestigationSegment] = field(default_factory=dict)

    def append_text(self, delta: str):
        if self.segments and isinstance(self.segments[-1], TextSegment):
            self.segments[-1].content += delta
        else:
            self.segments.append(TextSegment(content=delta))

    def start_investigation(
        self, iid, title, subtitle="", max_tool_calls=5, debug_mode=False
    ):
        seg = InvestigationSegment(
            id=iid,
            title=title,
            subtitle=subtitle,
            max_tool_calls=max_tool_calls,
            debug_mode=debug_mode,
        )
        self._investigations[iid] = seg
        self.segments.append(seg)

    def update_investigation(self, iid, query, tool_calls, max_tool_calls):
        if seg := self._investigations.get(iid):
            seg.add_query(query, tool_calls, max_tool_calls)

    def complete_investigation(
        self, iid, notes_count=0, sources_count=0, total_queries_run=0
    ):
        if seg := self._investigations.get(iid):
            seg.complete(notes_count, sources_count, total_queries_run)

    def add_reflection(self, content, reflection_type):
        self.segments.append(
            ReflectionSegment(content=content, reflection_type=reflection_type)
        )

    def add_memory(self, content, memory_type):
        self.segments.append(MemorySegment(content=content, memory_type=memory_type))

    def __rich_console__(self, console, options):
        for segment in self.segments:
            if isinstance(segment, TextSegment) and segment.content.strip():
                yield Markdown(segment.content)
            elif isinstance(
                segment,
                (
                    InvestigationSegment,
                    DirectionSegment,
                    ReflectionSegment,
                    MemorySegment,
                ),
            ):
                yield Text("")
                yield segment
                yield Text("")


class UIRenderer:
    def __init__(self, console: Console, debug_mode: bool = False):
        self.console = console
        self.debug_mode = debug_mode
        self.stream = ContentStream()
        self._live: Optional[Live] = None

    async def __aenter__(self):
        self.stream = ContentStream()
        self._live = Live(
            Panel(
                self.stream, title="Sage Response", border_style="cyan", padding=(1, 2)
            ),
            console=self.console,
            refresh_per_second=2,
            transient=False,
            vertical_overflow="visible",
        )
        self._live.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._live:
            self._live.stop()
            self._live = None
        return False

    def pause(self):
        if self._live:
            self._live.stop()

    def resume(self):
        if self._live:
            self._live.start()

    def on_text_delta(self, event: dict):
        self.stream.append_text(event.get("content", ""))

    def on_investigation_started(self, event: dict):
        iid = event.get("investigation_id") or event.get("research_question", "")
        short = event.get("short_question") or event.get("research_question", "")
        self.stream.start_investigation(
            iid,
            title=f"Investigation: {short}",
            subtitle=event.get("research_question", ""),
            debug_mode=self.debug_mode,
        )

    def on_investigation_query(self, event: dict):
        progress = event.get("progress", {})
        self.stream.update_investigation(
            event.get("investigation_id", ""),
            event.get("query", ""),
            progress.get("tool_calls", 0),
            progress.get("max_tool_calls", 5),
        )

    def on_investigation_complete(self, event: dict):
        iid = event.get("investigation_id") or event.get("research_question", "")
        self.stream.complete_investigation(
            iid,
            event.get("notes_count", 0),
            event.get("sources_count", 0),
            event.get("total_queries_run", 0),
        )

    def on_reflection_offered(self, event: dict):
        self.stream.add_reflection(
            event.get("content", ""), event.get("reflection_type", "question")
        )

    def on_memory_created(self, event: dict):
        self.stream.add_memory(
            event.get("content", ""), event.get("memory_type", "insight")
        )

    def on_generation_error(self, event: dict):
        self.stream.append_text(
            f"\n\n**Error:** {event.get('error', 'Unknown error')}\n"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Input Bar
# ═══════════════════════════════════════════════════════════════════════════

from prompt_toolkit.completion import Completer, Completion


class SlashCommandCompleter(Completer):
    COMMANDS = {
        "/page": "View recent thoughts",
        "/activity": "Thought contribution graph",
        "/explore": "Exploration questions",
        "/patterns": "Insight pattern analysis",
    }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd, desc in self.COMMANDS.items():
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text), display_meta=desc)


@dataclass
class InputContext:
    content_fn: Optional[Callable] = None
    setup_kb: Optional[Callable] = None
    hints: str = "enter submit"
    use_history: bool = False
    use_completer: bool = False
    format_result: Optional[Callable] = None


class InputBar:
    STYLE_DICT = {
        "hl": "bold cyan",
        "hl-label": "bold cyan reverse",
        "num": "cyan",
        "border": "#444444",
        "border-title": "bold cyan",
        "sep": "#444444",
        "dim": "#666666",
        "gq": "#666666",
        "gq-active": "bold yellow reverse",
    }

    def __init__(self, console: Console, history_file: str = "~/.sage/cli_history.txt"):
        self.console = console
        history_path = os.path.expanduser(history_file)
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        self.history = FileHistory(history_path)
        self.completer = SlashCommandCompleter()

    async def ask(self, ctx: Optional[InputContext] = None) -> Any:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            Float,
            FloatContainer,
            HSplit,
            Window,
        )
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.styles import Style

        ctx = ctx or InputContext(use_history=True, use_completer=True)
        buf = Buffer(
            history=self.history if ctx.use_history else None,
            completer=self.completer if ctx.use_completer else None,
            complete_while_typing=ctx.use_completer,
        )
        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event):
            event.app.exit(result=buf.text.strip() or None)

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event):
            event.app.exit(result=None)

        if ctx.setup_kb:
            ctx.setup_kb(kb, buf)

        windows = []
        if ctx.content_fn:
            windows.append(
                Window(
                    FormattedTextControl(ctx.content_fn),
                    dont_extend_height=True,
                    wrap_lines=True,
                )
            )
        windows.extend(
            [
                Window(height=1, char="─", style="class:sep"),
                Window(
                    BufferControl(buffer=buf, focusable=True),
                    height=3,
                    wrap_lines=True,
                    get_line_prefix=lambda ln, wc: (
                        [("class:dim", "  → ")] if not wc else [("", "     ")]
                    ),
                ),
                Window(
                    FormattedTextControl([("class:dim", f"  {ctx.hints}")]),
                    height=1,
                    dont_extend_height=True,
                ),
            ]
        )
        body = HSplit(windows)
        if ctx.use_completer:
            layout = Layout(
                FloatContainer(
                    content=body,
                    floats=[
                        Float(
                            xcursor=True,
                            ycursor=True,
                            content=CompletionsMenu(max_height=6),
                        )
                    ],
                )
            )
        else:
            layout = Layout(body)

        app = Application(
            layout=layout, key_bindings=kb, style=Style.from_dict(self.STYLE_DICT)
        )
        result = await app.run_async()

        if result is not None:
            sep = "─" * self.console.width
            self.console.print(f"[#444444]{sep}[/#444444]")
            if ctx.format_result:
                summary = ctx.format_result(result)
                if summary:
                    self.console.print(summary)
            self.console.print()
            self.console.print(f"[#444444]{sep}[/#444444]")
        return result


# ═══════════════════════════════════════════════════════════════════════════
# Main CLI
# ═══════════════════════════════════════════════════════════════════════════


class SageCLI:
    """Thin CLI client for Sage backend."""

    def __init__(
        self,
        use_deep_mode: bool = False,
        use_mobile_mode: bool = False,
        use_debug_mode: bool = False,
        use_local_mode: bool = False,
        api_url: str = "https://api.dev.sagethat.com",
    ):
        self.console = Console()
        self.use_deep_mode = use_deep_mode
        self.use_mobile_mode = use_mobile_mode
        self.use_debug_mode = use_debug_mode
        self.use_local_mode = use_local_mode

        modes = []
        if use_deep_mode:
            modes.append("Deep")
        if use_mobile_mode:
            modes.append("Mobile")
        if use_debug_mode:
            modes.append("Debug")
        if use_local_mode:
            modes.append("Local")
        self.mode_name = " + ".join(modes) if modes else "Standard"

        self.api_url = api_url
        self.backend: Optional[SageBackend] = None

        self.event_dispatcher = EventDispatcher()
        self.ui_renderer = UIRenderer(self.console, debug_mode=use_debug_mode)
        self.input_bar = InputBar(self.console)

        self.pending_selection: Optional[int] = None
        self.pending_user_input: Optional[str] = None
        self.pending_options: Optional[List[str]] = None
        self._pending_direction: Optional[DirectionSegment] = None
        self.last_guiding_questions: List[str] = []
        self.current_session_id: Optional[str] = None

        self.thought_cache: List[dict] = []
        self.thought_page: int = 1
        self.thought_page_size: int = 10

        self._setup_event_handlers()

    def _setup_event_handlers(self):
        ed = self.event_dispatcher
        ur = self.ui_renderer
        ed.on("text_delta", ur.on_text_delta)
        ed.on("investigation_started", ur.on_investigation_started)
        ed.on("investigation_query", ur.on_investigation_query)
        ed.on("investigation_complete", ur.on_investigation_complete)
        ed.on("reflection_offered", ur.on_reflection_offered)
        ed.on("memory_created", ur.on_memory_created)
        ed.on("generation_error", ur.on_generation_error)
        ed.on("paths_offered", self._on_paths_offered)

    async def _on_paths_offered(self, event: dict):
        options = event.get("options", [])
        self.ui_renderer.pause()
        selected = await self._select_option(options)
        if isinstance(selected, str):
            self._pending_direction = DirectionSegment(
                choices=options, user_text=selected
            )
            self.pending_selection, self.pending_user_input = None, selected
        elif selected is not None:
            self._pending_direction = DirectionSegment(
                choices=options, selected_index=selected
            )
            self.pending_selection, self.pending_user_input = selected, None
        else:
            self._pending_direction = DirectionSegment(choices=options, skipped=True)
            self.pending_selection, self.pending_user_input = None, None

    async def _select_option(self, options: list) -> "Optional[int | str]":
        selected = [0]

        def setup_kb(kb, buf):
            for idx in range(len(options)):
                letter = chr(ord("a") + idx)

                def make_handler(i):
                    def handler(event):
                        if buf.text:
                            buf.insert_text(event.data)
                        else:
                            event.app.exit(result=i)

                    return handler

                kb.add(letter, eager=True)(make_handler(idx))
                kb.add(letter.upper(), eager=True)(make_handler(idx))

            @kb.add("up", eager=True)
            def move_up(event):
                selected[0] = (selected[0] - 1) % len(options)

            @kb.add("down", eager=True)
            def move_down(event):
                selected[0] = (selected[0] + 1) % len(options)

            @kb.add("enter", eager=True)
            def confirm(event):
                text = buf.text.strip()
                event.app.exit(result=text if text else selected[0])

        def content_fn():
            lines = []
            for i, opt in enumerate(options):
                label = opt if isinstance(opt, str) else opt.get("label", str(opt))
                letter = chr(ord("A") + i)
                if i == selected[0]:
                    lines.append(("class:hl", f"  [{letter}] "))
                    lines.append(("class:hl-label", label))
                else:
                    lines.append(("", f"  [{letter}] "))
                    lines.append(("", label))
                lines.append(("", "\n"))
            return lines

        def format_result(result):
            if isinstance(result, int):
                letter = chr(ord("A") + result)
                label = (
                    options[result]
                    if isinstance(options[result], str)
                    else options[result].get("label", str(options[result]))
                )
                return f"[dim]→[/dim] [cyan][{letter}] {label}[/cyan]"
            if isinstance(result, str):
                return f"[dim]→[/dim] [cyan]{result}[/cyan]"
            return "[dim]→ skipped[/dim]"

        return await self.input_bar.ask(
            InputContext(
                content_fn=content_fn,
                setup_kb=setup_kb,
                hints="A/B/C select  ↑↓ navigate  enter confirm  type freely  esc skip",
                format_result=format_result,
            )
        )

    def display_banner(self):
        flags = []
        if self.use_deep_mode:
            flags.append("deep")
        if self.use_mobile_mode:
            flags.append("mobile")
        if self.use_local_mode:
            flags.append("local")
        label = "[bold cyan]Sage[/bold cyan]"
        if flags:
            label += f" [dim]({', '.join(flags)})[/dim]"
        self.console.print(label)
        self.console.print()

    async def _fetch_thoughts(self):
        if self.backend is None:
            return
        try:
            self.thought_cache = await self.backend.get_thoughts(limit=50)
        except SageBackendError as e:
            self.console.print(f"[red]Failed to load thoughts: {e}[/red]")

    def _get_thought_page(self) -> list:
        start = (self.thought_page - 1) * self.thought_page_size
        return self.thought_cache[start : start + self.thought_page_size]

    @property
    def total_pages(self) -> int:
        if not self.thought_cache:
            return 0
        return (
            len(self.thought_cache) + self.thought_page_size - 1
        ) // self.thought_page_size

    def display_thoughts(self):
        thoughts = self._get_thought_page()
        if not thoughts:
            return
        page_info = f"Page {self.thought_page}/{self.total_pages}"
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column(width=3, no_wrap=True, style="cyan")
        table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        table.add_column(no_wrap=True, style="dim", justify="right")
        for idx, t in enumerate(thoughts, 1):
            global_idx = (self.thought_page - 1) * self.thought_page_size + idx
            emotions = ", ".join(t.get("user_selected_emotions", [])[:2])
            table.add_row(f"{global_idx:2}.", t.get("text", ""), emotions)
        hint = f"Enter a number (1-{len(self.thought_cache)}) to reflect, or type your own."
        self.console.print(
            Panel(
                Group(table, Text(""), Text.from_markup(f"[dim]{hint}[/dim]")),
                title=f"[bold cyan]Recent Thoughts[/bold cyan] [dim]({page_info})[/dim]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def parse_input(self, user_input: str) -> tuple[str, Any]:
        stripped = user_input.strip()
        if stripped.startswith("/"):
            parts = stripped[1:].split()
            return "command", (parts[0].lower() if parts else "", parts[1:])
        if stripped.isdigit():
            num = int(stripped)
            if 1 <= num <= len(self.thought_cache):
                t = self.thought_cache[num - 1]
                page = ((num - 1) // self.thought_page_size) + 1
                if page != self.thought_page:
                    self.thought_page = page
                    self.display_thoughts()
                self.console.print(
                    f"[dim]📖 Responding to thought #{num}: {t.get('text', '')}[/dim]\n"
                )
                return "thought", (t.get("text", ""), t.get("journal_prompt"))
        return "thought", (user_input, None)

    async def handle_page_command(self, args: list):
        page = int(args[0]) if args and args[0].isdigit() else 1
        self.thought_page = max(1, min(page, max(1, self.total_pages)))
        self.display_thoughts()

    async def handle_patterns_command(self, args: list):
        if self.backend is None:
            return
        days = int(args[0]) if args and args[0].isdigit() else 30
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        ) as progress:
            task = progress.add_task(
                f"🔍 Analyzing patterns (past {days} days)...", total=None
            )
            try:
                data = await self.backend.get_insight_patterns(days=days)
                progress.update(task, completed=True)
                self._display_patterns(data.get("patterns", []), days)
            except SageBackendError as e:
                progress.update(task, completed=True)
                self.console.print(f"[red]Error: {e}[/red]")

    def _display_patterns(self, patterns: list, days: int):
        if not patterns:
            self.console.print(
                Panel(
                    "No significant patterns detected.",
                    title=f"📊 Patterns (Past {days} Days)",
                    border_style="yellow",
                    padding=(1, 2),
                )
            )
            return
        table = Table(
            show_header=False, box=None, padding=(1, 1), expand=True, show_lines=True
        )
        table.add_column(width=4, no_wrap=True)
        table.add_column(ratio=1)
        for idx, p in enumerate(patterns, 1):
            icon = PATTERN_ICONS.get(p.get("icon", ""), "✨")
            table.add_row(
                Text.from_markup(f"{icon} {idx}."),
                Text.from_markup(
                    f"[bold]{p.get('title', '')}[/bold]\n{p.get('what_it_is', '')}\n[dim]{p.get('what_it_means', '')}[/dim]"
                ),
            )
        self.console.print()
        self.console.print(
            Panel(
                table,
                title=f"📊 [bold cyan]Patterns[/bold cyan] [dim]({len(patterns)} patterns · past {days} days)[/dim]",
                border_style="cyan",
            )
        )

    async def process_thought(self, text: str, journal_prompt: Optional[str] = None):
        if self.backend is None:
            self.console.print(
                "[red]Not connected to backend. Run 'sage login' first.[/red]"
            )
            return

        max_resume_attempts = 10
        for attempt in range(max_resume_attempts):
            self.pending_selection = None
            self.pending_user_input = None

            async with self.ui_renderer:
                if self._pending_direction:
                    self.ui_renderer.stream.segments.append(self._pending_direction)
                    self._pending_direction = None

                deferred_seen = False
                try:
                    async for event in self.backend.stream_chat(
                        text=text,
                        deep_mode=self.use_deep_mode,
                        mobile_mode=self.use_mobile_mode,
                        journal_prompt=journal_prompt,
                        session_id=self.current_session_id,
                    ):
                        event_type = event.get("type", "")
                        if event_type == "complete":
                            sid = event.get("session_id")
                            if sid:
                                self.current_session_id = sid

                        if event_type == "paths_offered":
                            deferred_seen = True

                        if event_type == "complete":
                            gqs = event.get("guiding_questions", [])
                            if gqs:
                                self.last_guiding_questions = gqs
                                qmd = "\n\n---\n\n**Reflect**\n\n"
                                for i, q in enumerate(gqs, 1):
                                    qmd += f"{i}. {q.strip()}\n"
                                self.ui_renderer.stream.append_text(qmd)

                        await self.event_dispatcher.emit(event)
                        if event_type in (
                            "generation_complete",
                            "generation_error",
                            "paths_offered",
                        ):
                            break
                except SageBackendError as e:
                    self.console.print(f"[red]{e}[/red]")
                    return

                if deferred_seen:
                    if self.pending_selection is None and not self.pending_user_input:
                        self.console.print(
                            "[yellow]No selection made, conversation paused[/yellow]"
                        )
                        return

                    async with self.ui_renderer:
                        if self._pending_direction:
                            self.ui_renderer.stream.segments.append(
                                self._pending_direction
                            )
                            self._pending_direction = None

                        deferred_again = False
                        try:
                            async for event in self.backend.resume_session(
                                session_id=self.current_session_id or "",
                                selected_index=self.pending_selection,
                                user_input=self.pending_user_input,
                            ):
                                event_type = event.get("type", "")
                                if event_type == "complete":
                                    gqs = event.get("guiding_questions", [])
                                    if gqs:
                                        self.last_guiding_questions = gqs
                                        qmd = "\n\n---\n\n**Reflect**\n\n"
                                        for i, q in enumerate(gqs, 1):
                                            qmd += f"{i}. {q.strip()}\n"
                                        self.ui_renderer.stream.append_text(qmd)
                                if event_type == "paths_offered":
                                    deferred_again = True
                                await self.event_dispatcher.emit(event)
                                if event_type in (
                                    "generation_complete",
                                    "generation_error",
                                    "paths_offered",
                                    "deferred",
                                ):
                                    break
                        except SageBackendError as e:
                            self.console.print(f"[red]{e}[/red]")
                            return
                        if not deferred_again:
                            break
                    text = ""
                    continue
                break

    def _build_main_input_context(
        self, guiding_questions: List[str], default_hints: str
    ) -> InputContext:
        if not guiding_questions:
            return InputContext(
                use_history=True,
                use_completer=True,
                hints=default_hints,
                format_result=lambda r: f"  [dim]→[/dim] {r}",
            )
        selected = [None]
        n = len(guiding_questions)

        def content_fn():
            pills = [("class:dim", "  Reflect: ")]
            for i in range(n):
                if i == selected[0]:
                    pills.append(("class:gq-active", f" {i + 1} "))
                else:
                    pills.append(("class:gq", f" {i + 1} "))
                pills.append(("", "  "))
            return pills

        def setup_kb(kb, buf):
            @kb.add("up", eager=True)
            def _up(event):
                if not buf.text:
                    if selected[0] is None:
                        selected[0] = n - 1
                    elif selected[0] > 0:
                        selected[0] -= 1

            @kb.add("down", eager=True)
            def _down(event):
                if not buf.text:
                    if selected[0] is None:
                        selected[0] = 0
                    elif selected[0] < n - 1:
                        selected[0] += 1
                    else:
                        selected[0] = None

            @kb.add("enter", eager=True)
            def _enter(event):
                text = buf.text.strip()
                if text:
                    event.app.exit(result=text)
                elif selected[0] is not None:
                    event.app.exit(result=("gq", selected[0]))
                else:
                    event.app.exit(result=None)

        def format_result(result):
            if isinstance(result, tuple):
                return f"  [dim]→[/dim] [yellow]{guiding_questions[result[1]]}[/yellow]"
            return f"  [dim]→[/dim] {result}" if result else None

        return InputContext(
            content_fn=content_fn,
            setup_kb=setup_kb,
            hints="↑↓ select question  enter submit  type freely",
            use_history=True,
            use_completer=True,
            format_result=format_result,
        )

    async def run(self):
        # Resolve identity
        if self.use_local_mode:
            user_id = "anonymous"
            token = None
        else:
            auth_header = get_auth_header()
            if not auth_header:
                self.console.print(
                    "[red]Not logged in. Run 'sage login' or use --local for local dev.[/red]"
                )
                return
            import base64
            import json as _json

            token = auth_header.replace("Bearer ", "")
            try:
                payload = token.split(".")[1]
                payload += "=" * (4 - len(payload) % 4)
                claims = _json.loads(base64.urlsafe_b64decode(payload))
                user_id = claims.get("sub")
                if not user_id:
                    self.console.print(
                        "[red]Could not extract user ID from token.[/red]"
                    )
                    return
            except Exception:
                self.console.print(
                    "[red]Invalid authentication token. Run 'sage login' again.[/red]"
                )
                return

        self.backend = SageBackend(
            base_url=self.api_url, user_id=user_id, auth_token=token
        )

        def signal_handler(_sig, _frame):
            self.console.print(EXIT_MESSAGE)
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)

        self.display_banner()
        await self._fetch_thoughts()
        self.display_thoughts()

        main_hints = "enter submit  /page  /activity  /explore  /patterns"

        while True:
            try:
                gq = self.last_guiding_questions
                input_ctx = self._build_main_input_context(gq, main_hints)
                user_input = await self.input_bar.ask(input_ctx)

                active_gq = None
                if isinstance(user_input, tuple) and user_input[0] == "gq":
                    active_gq = gq[user_input[1]]
                    self.last_guiding_questions = []
                    self.console.print(
                        Panel(
                            active_gq,
                            title="Reflecting on",
                            border_style="yellow",
                            padding=(0, 1),
                        )
                    )
                    user_input = await self.input_bar.ask(
                        InputContext(
                            use_history=True,
                            hints="enter submit  esc cancel",
                            format_result=lambda r: f"  [dim]→[/dim] {r}",
                        )
                    )
                    if (
                        not user_input
                        or not isinstance(user_input, str)
                        or not user_input.strip()
                    ):
                        continue

                if (
                    not user_input
                    or not isinstance(user_input, str)
                    or not user_input.strip()
                ):
                    continue

                self.last_guiding_questions = []

                if user_input.lower() in ["quit", "exit", "q"]:
                    self.console.print(EXIT_MESSAGE)
                    break

                input_type, content = self.parse_input(user_input)
                if input_type == "command":
                    cmd, args = content
                    if cmd == "page":
                        await self.handle_page_command(args)
                    elif cmd == "patterns":
                        await self.handle_patterns_command(args)
                    elif cmd in ("activity", "explore"):
                        self.console.print(
                            f"[yellow]/{cmd} not yet available in thin CLI mode[/yellow]"
                        )
                    else:
                        self.console.print(f"[yellow]Unknown command: /{cmd}[/yellow]")
                    continue
                elif input_type == "thought":
                    text, journal_prompt = content
                    await self.process_thought(text, journal_prompt=journal_prompt)

            except KeyboardInterrupt:
                self.console.print(EXIT_MESSAGE)
                break
            except Exception as e:
                self.console.print(f"[red]Error: {e}[/red]")
                continue

    async def close(self):
        if self.backend:
            await self.backend.close()


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════


def parse_args():
    args = sys.argv[1:]
    if args and args[0] == "login":
        return "login", {}
    if args and args[0] == "logout":
        return "logout", {}
    flags = {
        "deep": "--deep" in args,
        "mobile": "--mobile" not in args or "--mobile" in args,  # default on
        "debug": "--debug" in args,
        "local": "--local" in args,
    }
    api_url = os.environ.get("SAGE_API_URL", "https://api.dev.sagethat.com")
    for arg in args:
        if arg.startswith("--api-url="):
            api_url = arg.split("=", 1)[1]
    flags["api_url"] = api_url
    return "run", flags


async def main():
    action, params = parse_args()
    if action == "login":
        auth_login()
        return
    if action == "logout":
        auth_logout()
        return
    cli = SageCLI(
        use_deep_mode=params["deep"],
        use_mobile_mode=params["mobile"],
        use_debug_mode=params["debug"],
        use_local_mode=params["local"],
        api_url=params["api_url"],
    )
    try:
        await cli.run()
    finally:
        await cli.close()


def main_entry():
    """Entry point for console_scripts."""
    import warnings

    warnings.filterwarnings("ignore", category=ResourceWarning)
    asyncio.run(main())


if __name__ == "__main__":
    main_entry()
