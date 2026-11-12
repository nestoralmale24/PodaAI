"""Tool-calling loop over LiteLLM with read-only tools and a turn budget."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from ..events import null_emit
from ..prompts import render
from ..schemas import schema_text
from .llm_engine import LLMEngine, parse_structured, _validation_error
from .tools import ReadOnlyTools

T = TypeVar("T", bound=BaseModel)
MAX_TOOL_CHARS_IN_CONTEXT = 240_000


def _prune(messages: List[Dict[str, Any]]) -> None:
    """Keep the conversation bounded: shorten the oldest tool outputs first."""
    total = sum(len(m.get("content") or "") for m in messages if m.get("role") == "tool")
    for message in messages:
        if total <= MAX_TOOL_CHARS_IN_CONTEXT:
            break
        if message.get("role") == "tool" and len(message.get("content") or "") > 400:
            before = len(message["content"])
            message["content"] = message["content"][:400] + "\n[... resultado antiguo recortado]"
            total -= before - len(message["content"])


async def run_agent(engine: LLMEngine, tools: ReadOnlyTools, *, system: str, task: str, schema: Type[T],
                    phase: str, label: str, max_turns: int = 25, model: Optional[str] = None,
                    pending_rule: str = "Return the complete JSON object.", emit=null_emit) -> Optional[T]:
    model = model or engine.cfg.model
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    key = engine.cache_key(model, messages, schema, "agent")
    cached = engine.cached(key, schema)
    if cached is not None:
        emit("activity", "↺ reutiliza el resultado de la ejecución anterior", label=label)
        return cached
    specs = tools.specs()
    repairs = 0
    exhausted = True
    for _turn in range(max_turns):
        reply = await engine.complete(messages, phase=phase, label=label, mode="agent", model=model, tools=specs)
        if reply.tool_calls:
            messages.append(reply.assistant_message)
            for call in reply.tool_calls:
                emit("tool_call", tools.describe(call.name, call.arguments), label=label, tool=call.name,
                     args=call.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": tools.run(call.name, call.arguments)})
            _prune(messages)
            continue
        result = parse_structured(reply.content, schema)
        if result is not None:
            engine.store(key, result)
            return result
        if repairs >= 1:
            exhausted = False
            break
        repairs += 1
        emit("activity", "↻ pide que corrija el formato de la respuesta", label=label)
        messages.append({"role": "assistant", "content": reply.content or "(vacío)"})
        messages.append({"role": "user", "content": render("repair", {
            "ERROR": _validation_error(reply.content, schema), "SCHEMA": schema_text(schema)})})
    emit("activity", "⏱ presupuesto de turnos agotado: pide el JSON final" if exhausted
         else "↻ respuesta sin JSON válido: pide el cierre con el JSON final", label=label)
    messages.append({"role": "user", "content": render("finalize", {
        "PENDING_RULE": pending_rule, "OUTPUT_LANGUAGE": engine.language})})
    reply = await engine.complete(messages, phase=phase, label=label, mode="agent", model=model, tools=specs,
                                  tool_choice="none")
    result = parse_structured(reply.content, schema)
    if result is not None:
        engine.store(key, result)
    return result
