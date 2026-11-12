"""Run metrics: phase timing, LLM usage and cost, lines of code before/after."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from .text import SYNTAXES, classify_lines, split_lines

LOC_KINDS = {"source", "web", "script", "db", "config", "build"}
# What the code count leaves out on purpose (shown next to it so the figure can be compared with cloc & co.).
NOT_COUNTED_LABELS = {"documentation": "documentación (.md, .txt, .adoc…)",
                      "other_text": "otros ficheros de texto (metadatos del IDE, etc.)"}
LOC_SCOPE = "sin comentarios, líneas en blanco, documentación ni metadatos del IDE"


@dataclass
class LLMCallRecord:
    phase: str
    label: str
    mode: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "unknown"
    latency_s: float = 0.0
    ok: bool = True
    error: Optional[str] = None
    tool_calls: int = 0


class MetricsTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.calls: List[LLMCallRecord] = []
        self.phases: Dict[str, Dict[str, float]] = {}
        self.reused_results = 0
        self.escalations = 0
        self.gateway_spend: Optional[Dict[str, float]] = None

    def phase_start(self, name: str) -> None:
        self.phases[name] = {"start": time.time()}

    def phase_end(self, name: str) -> float:
        phase = self.phases.setdefault(name, {"start": time.time()})
        phase["end"] = time.time()
        phase["seconds"] = round(phase["end"] - phase["start"], 2)
        return phase["seconds"]

    def record(self, record: LLMCallRecord) -> None:
        with self.lock:
            self.calls.append(record)

    def tokens(self) -> Dict[str, int]:
        with self.lock:
            return {"prompt": sum(c.prompt_tokens for c in self.calls),
                    "completion": sum(c.completion_tokens for c in self.calls),
                    "cached": sum(c.cached_tokens for c in self.calls)}

    def cost(self) -> Optional[float]:
        with self.lock:
            known = [c.cost_usd for c in self.calls if c.cost_usd is not None]
        return round(sum(known), 6) if known else None

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            calls = list(self.calls)
        by_model: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0,
                                                                    "completion_tokens": 0, "cost_usd": 0.0})
        by_phase: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0,
                                                                    "completion_tokens": 0, "cost_usd": 0.0,
                                                                    "tool_calls": 0})
        for call in calls:
            for bucket in (by_model[call.model], by_phase[call.phase]):
                bucket["calls"] += 1
                bucket["prompt_tokens"] += call.prompt_tokens
                bucket["completion_tokens"] += call.completion_tokens
                bucket["cost_usd"] = round(bucket["cost_usd"] + (call.cost_usd or 0.0), 6)
            by_phase[call.phase]["tool_calls"] += call.tool_calls
        sources = sorted({c.cost_source for c in calls if c.cost_usd is not None})
        return {
            "calls": len(calls),
            "failed_calls": sum(1 for c in calls if not c.ok),
            "reused_results": self.reused_results,
            "escalations_to_agent": self.escalations,
            "tool_calls": sum(c.tool_calls for c in calls),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "cached_tokens": sum(c.cached_tokens for c in calls),
            "cost_usd": self.cost(),
            "cost_sources": sources,
            "calls_without_cost": sum(1 for c in calls if c.ok and c.cost_usd is None),
            "by_model": dict(by_model),
            "by_phase": dict(by_phase),
            "detail": [asdict(c) for c in calls],
        }


def loc_stats(inv) -> Dict[str, Any]:
    """Physical and code lines (no blanks, no comment-only lines) per language."""
    per_lang: Dict[str, Dict[str, int]] = defaultdict(lambda: {"files": 0, "physical": 0, "code": 0})
    not_counted: Dict[str, Dict[str, Any]] = {k: {"files": 0, "lines": 0, "folders": []} for k in NOT_COUNTED_LABELS}
    for fi in inv.files:
        if fi.generated or not inv.is_text(fi):
            continue
        if fi.kind not in LOC_KINDS:
            text = inv.text(fi)
            if text is None:
                continue
            bucket = not_counted["documentation" if fi.kind == "doc" else "other_text"]
            bucket["files"] += 1
            bucket["lines"] += sum(1 for line in split_lines(text) if line.strip())
            folder = fi.rel.split("/", 1)[0] if "/" in fi.rel else fi.name
            if folder not in bucket["folders"] and len(bucket["folders"]) < 4:
                bucket["folders"].append(folder)
            continue
        text = inv.text(fi)
        if text is None:
            continue
        lines = split_lines(text)
        syntax = SYNTAXES.get(fi.syntax) if fi.syntax else None
        if syntax is not None and not any(len(line) > 3000 for line in lines[:50]):
            code = sum(1 for info in classify_lines(lines, syntax).lines if info.kind in ("code", "mixed"))
        else:
            code = sum(1 for line in lines if line.strip())
        lang = fi.ext or fi.name.lower()
        per_lang[lang]["files"] += 1
        per_lang[lang]["physical"] += len(lines)
        per_lang[lang]["code"] += code
    totals = {"files": sum(v["files"] for v in per_lang.values()),
              "physical": sum(v["physical"] for v in per_lang.values()),
              "code": sum(v["code"] for v in per_lang.values())}
    return {"totals": totals, "by_language": dict(sorted(per_lang.items(), key=lambda kv: -kv[1]["physical"])),
            "not_counted": {k: v for k, v in not_counted.items() if v["files"]}}


def not_counted_text(stats: Dict[str, Any]) -> str:
    """'documentación (.md…) 8 ficheros, 452 líneas · otros … 7 ficheros, 41 líneas (.settings, …)'."""
    parts = []
    for key, value in (stats.get("not_counted") or {}).items():
        where = f" en {', '.join(value['folders'])}" if key == "other_text" and value.get("folders") else ""
        lines = f"{value['lines']:,}".replace(",", ".")
        parts.append(f"{NOT_COUNTED_LABELS.get(key, key)}: {value['files']} ficheros, {lines} líneas{where}")
    return " · ".join(parts)
