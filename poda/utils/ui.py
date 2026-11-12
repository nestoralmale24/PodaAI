"""Terminal rendering of pipeline events.

* RichUI: live dashboard (phases with spinner and progress bar, what each worker
  is doing, the agent's latest reads and searches, counters) with the important
  lines (removals, verdicts, warnings) scrolling above it.
* PlainUI: one line per event, for CI, redirected output or --plain.
* FileLog: everything, as plain text, to run.log.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, List, Optional, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ..events import Event
from .formatting import fmt_int, fmt_secs, fmt_usd

PHASES = ["Inventario", "SonarQube", "Análisis estático", "Contexto del proyecto (IA)", "Verificación (IA)",
          "Descubrimiento (IA)", "Aplicación de cambios", "Informes"]
SCROLL_KINDS = {"phase_end", "phase_skip", "info", "warn", "error", "removed", "trimmed", "verdicts",
                "finding_summary", "header", "done", "detail"}
_MARKUP = re.compile(r"\[/?[a-z ]+\]")


def event_line(ev: Event) -> Optional[str]:
    """Plain text for an event, or None when it is only dashboard state."""
    kind, data = ev.kind, ev.data
    if kind == "phase_start":
        return f"▶ {data['index']}/{data['total']} · {data['title']}"
    if kind == "phase_end":
        summary = f" · {data['summary']}" if data.get("summary") else ""
        return f"✔ {data['title']} ({fmt_secs(data.get('seconds', 0))}){summary}"
    if kind == "phase_skip":
        return f"· {data['title']}: {ev.message}"
    if kind in ("info", "detail", "finding_summary", "header", "done"):
        return ev.message
    if kind == "warn":
        return f"⚠ {ev.message}"
    if kind == "error":
        return f"✗ {ev.message}"
    if kind in ("activity", "tool_call"):
        return f"[{data.get('label', '')}] {ev.message}"
    if kind == "verdicts":
        text = (f"[{data['label']}] veredictos: {data['remove']} eliminar · {data['keep']} conservar · "
                f"{data['unsure']} dudosos")
        return text + (f" · {data['escalate']} pasan al agente" if data.get("escalate") else "")
    if kind == "removed":
        return f"🗑  {ev.message}"
    if kind == "trimmed":
        return f"✂  {ev.message}"
    return None


class FileLog:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def __call__(self, ev: Event) -> None:
        line = event_line(ev)
        if line is None:
            return
        with self.lock:
            self.stream.write(time.strftime("%H:%M:%S ", time.localtime(ev.ts)) + _MARKUP.sub("", line) + "\n")
            self.stream.flush()


class PlainUI:
    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or Console(highlight=False, no_color=True, soft_wrap=True)
        self.lock = threading.Lock()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def __call__(self, ev: Event) -> None:
        line = event_line(ev)
        if line is None:
            return
        prefix = time.strftime("[%H:%M:%S] ", time.localtime(ev.ts)) if ev.kind in ("phase_start", "done") else "   "
        with self.lock:
            self.console.print(prefix + line, markup=False, highlight=False, soft_wrap=True)


class RichUI:
    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or Console(highlight=False)
        self.lock = threading.RLock()
        self.started = time.time()
        self.phases: Dict[int, Dict[str, Any]] = {
            i: {"title": t, "status": "pending", "summary": "", "seconds": None} for i, t in enumerate(PHASES, 1)}
        self.progress: Dict[str, tuple] = {}
        self.current_phase: Optional[int] = None
        self.activities: "OrderedDict[str, str]" = OrderedDict()
        self.recent_tools: Deque[str] = deque(maxlen=6)
        self.counters: Dict[str, Any] = {"candidates": 0, "removed_files": 0, "trimmed_lines": 0,
                                         "verdicts": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                         "cost": None}
        self.live: Optional[Live] = None

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        self.live = Live(console=self.console, refresh_per_second=8, get_renderable=self.render,
                         redirect_stdout=False, redirect_stderr=False)
        self.live.start()

    def stop(self) -> None:
        if self.live is not None:
            with self.lock:
                self.activities.clear()
            self.live.refresh()
            self.live.stop()
            self.live = None

    # -- events --------------------------------------------------------------------------
    def __call__(self, ev: Event) -> None:
        kind, data = ev.kind, ev.data
        with self.lock:
            if kind == "phase_start":
                self.current_phase = data["index"]
                self.phases[data["index"]]["status"] = "running"
                self.activities.clear()
            elif kind == "phase_end":
                phase = self.phases[data["index"]]
                phase.update(status="done", seconds=data.get("seconds"), summary=data.get("summary", ""))
            elif kind == "phase_skip":
                self.phases[data["index"]].update(status="skipped", summary=ev.message)
            elif kind == "progress":
                self.progress[data["phase"]] = (data.get("done", 0), max(1, data.get("total", 1)))
            elif kind in ("activity", "tool_call", "llm_call"):
                label = data.get("label") or "·"
                text = ev.message or (f"🤖 consulta a {data.get('model')} ({data.get('mode')})" if kind == "llm_call" else "")
                self.activities[label] = text
                self.activities.move_to_end(label)
                while len(self.activities) > 6:
                    self.activities.popitem(last=False)
                if kind == "tool_call":
                    self.recent_tools.append(f"[{label}] {ev.message}")
            elif kind == "usage":
                self.counters["prompt_tokens"] = data.get("prompt_tokens", 0)
                self.counters["completion_tokens"] = data.get("completion_tokens", 0)
                self.counters["cost"] = data.get("cost")
            elif kind == "finding_summary" and data.get("count"):
                self.counters["candidates"] += data.get("count", 0)
            elif kind == "verdicts":
                self.counters["verdicts"] += data.get("remove", 0) + data.get("keep", 0) + data.get("unsure", 0)
            elif kind == "removed":
                self.counters["removed_files"] += data.get("files", 1)
            elif kind == "trimmed":
                self.counters["trimmed_lines"] += data.get("lines", 0)
        if kind in SCROLL_KINDS:
            line = event_line(ev)
            if line is not None:
                style = {"warn": "yellow", "error": "bold red", "phase_end": "green", "removed": "",
                         "trimmed": "", "detail": "dim", "phase_skip": "dim"}.get(kind, "")
                text = Text("   " + line, style=style) if kind != "header" else Text(line, style="bold cyan")
                self.console.print(text)

    # -- rendering ------------------------------------------------------------------------
    def render(self):
        with self.lock:
            phases = {i: dict(p) for i, p in self.phases.items()}
            activities = list(self.activities.items())
            tools = list(self.recent_tools)
            counters = dict(self.counters)
            progress = dict(self.progress)
            current = self.current_phase
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(min_width=28)
        table.add_column()
        keys = {3: "static", 5: "verify", 6: "discover", 7: "apply"}
        for index, phase in phases.items():
            status = phase["status"]
            if status == "running":
                icon = Spinner("dots", style="cyan")
                done_total = progress.get(keys.get(index, ""))
                detail = ProgressBar(total=done_total[1], completed=done_total[0], width=28) if done_total \
                    else Text(phase["summary"], style="dim")
                table.add_row(icon, Text(phase["title"], style="bold"), detail)
            elif status == "done":
                summary = " · ".join(x for x in (phase["summary"], fmt_secs(phase["seconds"])
                                                 if phase["seconds"] else "") if x)
                table.add_row(Text("✔", style="green"), Text(phase["title"]), Text(summary, style="dim"))
            elif status == "skipped":
                table.add_row(Text("⤼", style="dim"), Text(phase["title"], style="dim"),
                              Text(phase["summary"], style="dim"))
            else:
                table.add_row(Text("·", style="dim"), Text(phase["title"], style="dim"), Text(""))
        parts: List[Any] = [table]
        if activities and current is not None:
            act = Table.grid(padding=(0, 1))
            act.add_column(style="magenta", no_wrap=True)
            act.add_column(overflow="ellipsis", no_wrap=True)
            for label, text in activities[-4:]:
                act.add_row(f"[{label}]", text)
            parts.append(Text(""))
            parts.append(act)
        if tools:
            parts.append(Text("\n".join(tools[-4:]), style="dim", overflow="ellipsis", no_wrap=True))
        cost = counters["cost"]
        footer = (f"hallazgos {fmt_int(counters['candidates'])} · veredictos {fmt_int(counters['verdicts'])} · "
                  f"🗑 {fmt_int(counters['removed_files'])} ficheros · ✂ {fmt_int(counters['trimmed_lines'])} líneas · "
                  f"tokens {fmt_int(counters['prompt_tokens'])}/{fmt_int(counters['completion_tokens'])} · "
                  f"coste {fmt_usd(cost) if cost is not None else 'n/d'} · ⏱ {fmt_secs(time.time() - self.started)}")
        parts.append(Text(""))
        parts.append(Text(footer, style="bold"))
        return Panel(Group(*parts), title="Poda AI", border_style="cyan", padding=(0, 1))
