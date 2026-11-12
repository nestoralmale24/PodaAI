"""The only place that talks to the LLM, through LiteLLM.

Responsibilities: provider/model parameters, structured output (JSON Schema
when the provider supports it, validated with pydantic), one repair attempt for
malformed answers, retries, error classification (fatal errors stop every AI
phase), token and cost accounting, and a result cache for --resume.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import LLMConfig
from ..events import null_emit
from ..prompts import render
from ..providers import copilot_pricing
from ..schemas import schema_text
from ..utils.metrics import LLMCallRecord, MetricsTracker

T = TypeVar("T", bound=BaseModel)
Transport = Callable[..., Awaitable[Any]]

FATAL_PATTERN = re.compile(
    r"\b40[13]\b|authenticat|unauthori[sz]ed|invalid.{0,12}(api.?key|token)|expired.?token|"
    r"budget|quota|insufficient|credit balance|billing|copilot login|"
    r"model.{0,40}(not found|does not exist|not available|not allowed)|invalid model|no such model|"
    r"getaddrinfo|name or service not known|nodename nor servname|connection refused|"
    r"certificate verify failed|ssl", re.I)


class LLMFatalError(RuntimeError):
    """The provider cannot serve any call (credentials, model, budget, network)."""


class LLMCallError(RuntimeError):
    """This call failed; other calls may still work."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    raw_arguments: str


@dataclass
class LLMReply:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    assistant_message: Dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "unknown"
    model: str = ""
    finish_reason: Optional[str] = None


def load_transport(spec: str) -> Transport:
    """`module:function` (used by the tests to replace LiteLLM)."""
    module_name, _, attr = spec.partition(":")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    return getattr(importlib.import_module(module_name), attr or "transport")


def extract_json_objects(text: str) -> List[Any]:
    """Every JSON object found in a text (fenced blocks first)."""
    if not text:
        return []
    chunks = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)] + [text]
    decoder = json.JSONDecoder()
    found: List[Any] = []
    for chunk in chunks:
        chunk = chunk.strip()
        index = 0
        while True:
            start = chunk.find("{", index)
            if start < 0:
                break
            try:
                obj, end = decoder.raw_decode(chunk, start)
            except ValueError:
                index = start + 1
                continue
            found.append(obj)
            index = end
    return found


def parse_structured(text: str, schema: Type[T]) -> Optional[T]:
    for obj in extract_json_objects(text):
        if isinstance(obj, dict):
            try:
                return schema.model_validate(obj)
            except ValidationError:
                continue
    return None


def _validation_error(text: str, schema: Type[BaseModel]) -> str:
    objects = [o for o in extract_json_objects(text) if isinstance(o, dict)]
    if not objects:
        return "no contenía ningún objeto JSON"
    try:
        schema.model_validate(objects[0])
    except ValidationError as exc:
        return "no cumple el esquema: " + "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
    return "formato no válido"


class LLMEngine:
    def __init__(self, cfg: LLMConfig, metrics: MetricsTracker, emit=null_emit, *,
                 cache_path: Optional[Path] = None, resume: bool = False, max_output_tokens: int = 8000,
                 timeout: int = 600, price_in: Optional[float] = None, price_out: Optional[float] = None,
                 language: str = "Spanish", transport: Optional[Transport] = None,
                 tools_enabled: Optional[bool] = None) -> None:
        import litellm
        litellm.drop_params = True
        litellm.suppress_debug_info = True
        if not os.environ.get("LITELLM_LOG"):
            # Failures are reported through Poda AI's own events and run.log; LiteLLM's
            # stderr logging would only break the live dashboard (LITELLM_LOG=DEBUG restores it).
            for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
                logging.getLogger(name).setLevel(logging.CRITICAL)
        self._litellm = litellm
        self.cfg = cfg
        self.metrics = metrics
        self.emit = emit
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.price_in, self.price_out = price_in, price_out
        self.language = language
        spec = os.environ.get("PODA_LLM_TRANSPORT")
        self.transport: Transport = transport or (load_transport(spec) if spec else litellm.acompletion)
        self.fatal: Optional[str] = None
        self.cancel: Optional[threading.Event] = None
        self.cache_path = cache_path
        self.cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        if resume and cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except ValueError:
                self.cache = {}
        self._schema_ok: Dict[str, bool] = {}
        self._tools_enabled = tools_enabled

    # -- capabilities -----------------------------------------------------------------
    def tools_supported(self, model: Optional[str] = None) -> bool:
        if self._tools_enabled is not None:
            return self._tools_enabled
        if self.cfg.provider in ("litellm", "copilot", "github_copilot"):
            return True  # gateway aliases are unknown to LiteLLM's capability table
        try:
            return bool(self._litellm.supports_function_calling(model=model or self.cfg.model))
        except Exception:
            return True

    def _schema_supported(self, model: str) -> bool:
        if model not in self._schema_ok:
            try:
                self._schema_ok[model] = bool(self._litellm.supports_response_schema(model=model))
            except Exception:
                self._schema_ok[model] = False
        return self._schema_ok[model]

    # -- cache --------------------------------------------------------------------------
    def cache_key(self, model: str, messages: List[Dict[str, Any]], schema: Type[BaseModel], mode: str) -> str:
        payload = json.dumps({"model": model, "mode": mode, "schema": schema.__name__, "messages": messages},
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def cached(self, key: str, schema: Type[T]) -> Optional[T]:
        data = self.cache.get(key)
        if data is None:
            return None
        try:
            result = schema.model_validate(data)
        except ValidationError:
            return None
        self.metrics.reused_results += 1
        return result

    def store(self, key: str, result: BaseModel) -> None:
        with self._lock:
            self.cache[key] = result.model_dump()
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    # -- calls ---------------------------------------------------------------------------
    async def complete(self, messages: List[Dict[str, Any]], *, phase: str, label: str, mode: str,
                       model: Optional[str] = None, tools: Optional[List[Dict[str, Any]]] = None,
                       tool_choice: Optional[str] = None,
                       schema: Optional[Type[BaseModel]] = None) -> LLMReply:
        if self.cancel is not None and self.cancel.is_set():
            self.fatal = self.fatal or "ejecución detenida a petición del usuario"
        if self.fatal:
            raise LLMFatalError(self.fatal)
        model = model or self.cfg.model
        kwargs: Dict[str, Any] = dict(self.cfg.call_kwargs())
        kwargs.update(model=model, messages=messages, temperature=0, max_tokens=self.max_output_tokens,
                      timeout=self.timeout, num_retries=2, _skip_mcp_handler=True)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if schema is not None and not tools and self._schema_supported(model):
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}}
        started = time.monotonic()
        try:
            response = await self.transport(**kwargs)
        except Exception as exc:  # noqa: BLE001 - classified below
            if "response_format" in kwargs and type(exc).__name__ in ("BadRequestError", "UnsupportedParamsError"):
                self._schema_ok[model] = False
                kwargs.pop("response_format")
                try:
                    response = await self.transport(**kwargs)
                except Exception as retry_exc:  # noqa: BLE001
                    exc = retry_exc
                    response = None
            else:
                response = None
            if response is None:
                return self._failure(exc, phase, label, mode, model, started)
        reply = self._normalize(response, model)
        record = LLMCallRecord(phase=phase, label=label, mode=mode, model=model,
                               prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens,
                               cached_tokens=reply.cached_tokens, cost_usd=reply.cost_usd,
                               cost_source=reply.cost_source, latency_s=round(time.monotonic() - started, 2),
                               tool_calls=len(reply.tool_calls))
        self.metrics.record(record)
        tokens = self.metrics.tokens()
        self.emit("usage", label=label, model=model, prompt_tokens=tokens["prompt"],
                  completion_tokens=tokens["completion"], cost=self.metrics.cost(),
                  estimated=reply.cost_source == "copilot_estimate")
        return reply

    def _failure(self, exc: Exception, phase: str, label: str, mode: str, model: str, started: float):
        message = f"{type(exc).__name__}: {exc}"
        secret = self.cfg.api_key
        if secret:
            message = message.replace(secret, "***")
        message = message[:800]
        self.metrics.record(LLMCallRecord(phase=phase, label=label, mode=mode, model=model, ok=False,
                                          error=message, latency_s=round(time.monotonic() - started, 2)))
        fatal_types = ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "BudgetExceededError")
        if type(exc).__name__ in fatal_types or (type(exc).__name__ in ("APIConnectionError", "APIError",
                                                                          "BadRequestError")
                                                  and FATAL_PATTERN.search(str(exc))):
            self.fatal = message
            self.emit("error", f"error del proveedor LLM, se detiene la IA: {message}")
            raise LLMFatalError(message) from exc
        self.emit("warn", f"[{label}] llamada fallida: {message}")
        raise LLMCallError(message) from exc

    def _normalize(self, response: Any, model: str) -> LLMReply:
        choice = response.choices[0]
        message = choice.message
        content = message.content if isinstance(message.content, str) else (message.content or "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        calls: List[ToolCall] = []
        for call in (getattr(message, "tool_calls", None) or []):
            raw = call.function.arguments or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=call.id or f"call_{len(calls)}", name=call.function.name or "",
                                  arguments=args, raw_arguments=raw if isinstance(raw, str) else json.dumps(raw)))
        assistant: Dict[str, Any] = {"role": "assistant", "content": content or ""}
        if calls:
            assistant["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.name, "arguments": c.raw_arguments}} for c in calls]
        usage = getattr(response, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        cost, source = self._cost(response, prompt, completion, cached, model)
        return LLMReply(content=content or "", tool_calls=calls, assistant_message=assistant,
                        prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached,
                        cost_usd=cost, cost_source=source, model=model,
                        finish_reason=getattr(choice, "finish_reason", None))

    def _cost(self, response: Any, prompt: int, completion: int, cached: int = 0, model: str = ""):
        if self.price_in is not None and self.price_out is not None:
            return round(prompt / 1e6 * self.price_in + completion / 1e6 * self.price_out, 8), "price_table"
        if self.cfg.provider in ("copilot", "github_copilot"):
            # GitHub AI Credits: tokens at GitHub's published rate for the model (an estimate).
            usd = copilot_pricing.estimate(model or self.cfg.model, prompt, completion, cached)
            return (usd, "copilot_estimate") if usd is not None else (None, "copilot_subscription")
        hidden = getattr(response, "_hidden_params", None) or {}
        headers = hidden.get("additional_headers") or {}
        for key, value in headers.items():
            if "x-litellm-response-cost" in str(key).lower():
                try:
                    return float(value), "gateway"
                except (TypeError, ValueError):
                    pass
        value = hidden.get("response_cost")
        if isinstance(value, (int, float)) and value > 0:
            return float(value), "litellm"
        try:
            value = self._litellm.completion_cost(completion_response=response)
            if value:
                return float(value), "litellm"
        except Exception:
            pass
        return None, "unknown"

    async def judge(self, messages: List[Dict[str, Any]], schema: Type[T], *, phase: str, label: str,
                    mode: str, model: Optional[str] = None) -> Optional[T]:
        """One call without tools that must return `schema`; one repair attempt."""
        model = model or self.cfg.model
        key = self.cache_key(model, messages, schema, mode)
        cached = self.cached(key, schema)
        if cached is not None:
            self.emit("activity", "↺ reutiliza el resultado de la ejecución anterior", label=label)
            return cached
        reply = await self.complete(messages, phase=phase, label=label, mode=mode, model=model, schema=schema)
        result = parse_structured(reply.content, schema)
        if result is None:
            repair = render("repair", {"ERROR": _validation_error(reply.content, schema),
                                       "SCHEMA": schema_text(schema)})
            self.emit("activity", "↻ pide que corrija el formato de la respuesta", label=label)
            retry = await self.complete(messages + [{"role": "assistant", "content": reply.content or "(vacío)"},
                                                    {"role": "user", "content": repair}],
                                        phase=phase, label=label, mode="repair", model=model, schema=schema)
            result = parse_structured(retry.content, schema)
        if result is not None:
            self.store(key, result)
        return result
