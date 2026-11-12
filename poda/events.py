"""Event bus: the core reports what it does, renderers decide how to show it.

Kinds used by the pipeline (data keys in brackets):
    phase_start [index, total, title]      phase_end [index, title, seconds, summary]
    progress    [phase, done, total]        activity [label, text]
    info / detail / warn / error            finding_summary [category, count, auto, verify]
    tool_call   [label, tool, args]         llm_call [label, model, mode]
    usage       [label, model, prompt_tokens, completion_tokens, cost]
    verdicts    [label, remove, keep, unsure, escalate]
    removed     [path, reason, files]       trimmed [path, lines, what]
    done        [summary]
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class Event:
    kind: str
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: List[Subscriber] = []
        self._lock = threading.RLock()

    def subscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(subscriber)

    def emit(self, kind: str, message: str = "", **data: Any) -> None:
        event = Event(kind=kind, message=message, data=data)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # a renderer must never break the pipeline
                pass


def null_emit(kind: str, message: str = "", **data: Any) -> None:
    """Emitter that discards everything (tests and library use)."""
