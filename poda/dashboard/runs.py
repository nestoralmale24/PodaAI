"""Runs started from the dashboard: apps under target/, results under output/, one run at a time, live events.

The pipeline runs in a background thread with the same code as `poda run`;
its events are kept in memory so the page can poll them (``events(since)``).
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import sonar_setup
from ..config import PROJECT_ROOT, ConfigError, default_output_dir, load_env, resolve_llm
from ..events import Event, EventBus
from ..pipeline import Pipeline, PipelineCancelled, PipelineError, RunOptions
from ..utils.ui import event_line

OUTPUT_SUFFIX = "-poda"   # outputs that older versions left next to the app, inside target/
OUTPUT_FILES = ("poda_report.md", "execution_metrics.md", "execution_metrics.json", "findings.json", "run.log")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 20000


class RunError(RuntimeError):
    pass


def target_dir() -> Path:
    raw = os.environ.get("PODA_TARGET_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else (PROJECT_ROOT / "target").resolve()


def _is_output(path: Path) -> bool:
    """Outputs left in target/ by older versions (also the ones from when the tool was called deadcode)."""
    return path.name.endswith((OUTPUT_SUFFIX, "-deadcode")) or (path / ".poda").is_dir() \
        or (path / ".deadcode").is_dir()


def output_dir(project: Path) -> Path:
    return default_output_dir(project)


def _summary(out: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((out / "execution_metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    changes, loc, cost = data.get("changes") or {}, data.get("loc") or {}, data.get("cost") or {}
    return {"finished_at": (data.get("timing") or {}).get("finished_at"), "dry_run": data.get("dry_run"),
            "files_deleted": changes.get("files_deleted"), "lines_removed": changes.get("lines_removed"),
            "loc_reduced_pct": (loc.get("reduced_pct") or {}).get("code"), "cost_usd": cost.get("total_usd"),
            "model": (data.get("llm") or {}).get("model")}


def list_projects() -> List[Dict[str, Any]]:
    root = target_dir()
    if not root.is_dir():
        return []
    projects = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_dir() or path.name.startswith(".") or _is_output(path):
            continue
        out = output_dir(path)
        has_output = (out / ".poda").is_dir()
        projects.append({"name": path.name, "has_output": has_output,
                         "last_run": _summary(out) if has_output else None})
    return projects


def project_path(name: str) -> Path:
    """Resolve a project name to a directory directly under target/ (no traversal)."""
    name = str(name or "").strip()
    root = target_dir()
    candidate = (root / name).resolve()
    if not name or "/" in name or "\\" in name or candidate.parent != root or not candidate.is_dir() \
            or _is_output(candidate) or name.startswith("."):
        raise RunError(f"No existe la aplicación «{name}» en {root}.")
    return candidate


def read_output(name: str, filename: str) -> Dict[str, Any]:
    if filename not in OUTPUT_FILES:
        raise RunError(f"Fichero no disponible: {filename}")
    path = output_dir(project_path(name)) / filename
    if not path.is_file():
        raise RunError(f"Todavía no hay {filename} para {name}.")
    data = path.read_bytes()
    truncated = len(data) > MAX_FILE_BYTES
    return {"name": filename, "path": str(path), "truncated": truncated,
            "content": data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@dataclass
class Run:
    id: str
    project: str
    options: Dict[str, Any]
    status: str = "starting"          # starting | running | done | failed | cancelled
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    exit_code: Optional[int] = None
    error: str = ""
    out_dir: str = ""
    llm: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    pipeline: Optional[Pipeline] = None

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "project": self.project, "options": self.options, "status": self.status,
                "started": self.started, "finished": self.finished, "exit_code": self.exit_code,
                "error": self.error, "out_dir": self.out_dir, "llm": self.llm, "event_count": len(self.events),
                "summary": _summary(Path(self.out_dir)) if self.out_dir and self.status == "done" else None}


class RunManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: Optional[Run] = None
        self.counter = 0

    def start(self, project: str, options: Dict[str, Any]) -> Run:
        path = project_path(project)
        with self.lock:
            if self.current and self.current.status in ("starting", "running"):
                raise RunError(f"Ya hay una ejecución en curso ({self.current.project}). Espera a que termine o detenla.")
            no_llm = False  # from the dashboard a provider is mandatory
            try:
                llm = resolve_llm(load_env())
            except ConfigError as exc:
                raise RunError(f"Configura primero un proveedor y un modelo. {exc}") from exc
            sonar = sonar_setup.status(max_age=0)
            if sonar["state"] != "ready":
                raise RunError(f"SonarQube no está listo: {sonar['message']}")
            out = output_dir(path)
            previous = (out / ".poda").is_dir()
            mode = str(options.get("previous") or "resume")
            opts = RunOptions(
                # One way to run from the dashboard: the real one (discovery on, clean copy written,
                # only what the AI confirms with high confidence is removed).
                project=path, out=out, no_llm=no_llm, no_discover=False,
                dry_run=False, resume=previous and mode == "resume",
                force=previous and mode == "force",
                min_confidence="high",
                jobs=max(1, min(8, int(options.get("jobs") or 2))), sonar=True, sonar_required=True)
            self.counter += 1
            run = Run(id=f"run-{self.counter}-{int(time.time())}", project=path.name,
                      options={"no_llm": no_llm, "no_discover": opts.no_discover, "dry_run": opts.dry_run,
                               "min_confidence": opts.min_confidence, "resume": opts.resume, "force": opts.force,
                               "jobs": opts.jobs},
                      out_dir=str(out), llm=llm.display if llm else "sin IA")
            bus = EventBus()
            bus.subscribe(lambda ev: self._collect(run, ev))
            run.pipeline = Pipeline(opts, bus, load_env(), llm)
            self.current = run
        threading.Thread(target=self._execute, args=(run,), daemon=True, name=f"poda-{run.id}").start()
        return run

    def _collect(self, run: Run, ev: Event) -> None:
        if len(run.events) >= MAX_EVENTS and ev.kind not in ("phase_start", "phase_end", "done", "error"):
            return
        run.events.append({"seq": len(run.events), "kind": ev.kind, "message": ev.message,
                           "line": event_line(ev), "data": _jsonable(ev.data), "ts": ev.ts})

    def _execute(self, run: Run) -> None:
        run.status = "running"
        try:
            run.exit_code = run.pipeline.run()
            run.status = "done"
            if run.pipeline.fatal:
                run.error = f"La IA se detuvo por un error del proveedor: {run.pipeline.fatal[:400]}"
        except PipelineCancelled as exc:
            run.status, run.error = "cancelled", str(exc)
        except PipelineError as exc:
            run.status, run.error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - the page must learn why
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
            run.events.append({"seq": len(run.events), "kind": "error", "message": traceback.format_exc(limit=3),
                               "line": f"✗ {type(exc).__name__}: {exc}", "data": {}, "ts": time.time()})
        finally:
            run.finished = time.time()

    def cancel(self) -> Run:
        run = self.current
        if not run or run.status not in ("starting", "running") or not run.pipeline:
            raise RunError("No hay ninguna ejecución en curso.")
        run.pipeline.cancel.set()
        return run

    def get(self, run_id: Optional[str] = None) -> Optional[Run]:
        run = self.current
        if run is None or (run_id and run.id != run_id):
            return None
        return run


RUNS = RunManager()
