"""Claude through the official `claude` CLI, with a login that belongs to Poda AI.

Poda AI never reads, copies or sends the account's OAuth token. It runs the CLI with its own
`CLAUDE_CONFIG_DIR` (`<settings>/claude`), so the session is independent of whatever account the computer (or
the host, when running in Docker) is signed in with. Sign-in is `claude auth login` (Google or e-mail, in the
browser, pasting back the code the page shows) and every call is a headless `claude -p` subprocess. It is a
LiteLLM-compatible transport: it takes the same keyword arguments as `litellm.acompletion` and returns an object
shaped like its response.

Limits: no function calling (so no AI agent escalation, only snippet verification), usage counts against the
subscription's own limits and there is no per-token bill.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

# Models worth asking the account about, newest first. The CLI has no "list models" command, so each one is
# probed with a one-word call; only the ones the account can really use are offered.
CANDIDATES = [
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-2",
    "claude-sonnet-5", "claude-opus-5", "claude-haiku-5",
    "claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5", "claude-opus-4-1", "claude-sonnet-4-0",
]
PREFIX = "claude/"
_CONCURRENCY = 3
_LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "quota", "too many requests")
_semaphore: Optional[asyncio.Semaphore] = None
_OSC8 = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https?://[^\x07\x1b]+)")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL = re.compile(r"https://\S+")


class AuthenticationError(RuntimeError):
    """Not signed in (the engine treats this class name as fatal)."""


class BudgetExceededError(RuntimeError):
    """The subscription's usage limit was reached (fatal for the run: every later call would fail too)."""


def executable() -> Optional[str]:
    return shutil.which("claude")


def config_dir() -> Path:
    """Where Poda AI's own Claude session lives (`$PODA_CLAUDE_HOME`, else `<settings>/claude`)."""
    raw = os.environ.get("PODA_CLAUDE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    from ..llm_settings import settings_dir
    return settings_dir() / "claude"


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    # An API key in the environment would silently switch the CLI away from the subscription.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(directory)
    return env


def auth_status(timeout: int = 20) -> Dict[str, Any]:
    """{'installed', 'logged_in', 'account', 'method'}; never any token."""
    exe = executable()
    if not exe:
        return {"installed": False, "logged_in": False, "account": "", "method": ""}
    try:
        done = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=timeout,
                              env=_env(), cwd=tempfile.gettempdir())
        data = json.loads(done.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"installed": True, "logged_in": False, "account": "", "method": ""}
    return {"installed": True, "logged_in": bool(data.get("loggedIn")), "account": str(data.get("email") or ""),
            "method": str(data.get("authMethod") or "")}


# ── sign-in: `claude auth login` with the page in the middle ─────────────────────────────

_FLOWS: Dict[str, Dict[str, Any]] = {}
_FLOWS_LOCK = threading.Lock()


def _login_url(text: str) -> str:
    found = _OSC8.search(text)
    if found:
        return found.group(1)
    match = _URL.search(_ANSI.sub("", text))
    return match.group(0) if match else ""


def _reader(flow: Dict[str, Any]) -> None:
    proc = flow["proc"]
    for chunk in iter(lambda: proc.stdout.read1(256), b""):  # read1: return what is there, do not wait for 256 bytes
        with flow["lock"]:
            flow["raw"] += chunk.decode("utf-8", errors="replace")
    proc.wait()


def _end_flow(flow_id: str) -> None:
    with _FLOWS_LOCK:
        flow = _FLOWS.pop(flow_id, None)
    if flow and flow["proc"].poll() is None:
        flow["proc"].kill()


def start_login() -> Dict[str, Any]:
    """Run `claude auth login` and hand back the sign-in page; the user pastes back the code it shows."""
    exe = executable()
    if not exe:
        raise RuntimeError("No se encuentra el comando `claude`. Instala Claude Code y vuelve a intentarlo.")
    for old in list(_FLOWS):
        _end_flow(old)
    env = _env()
    env.setdefault("BROWSER", "")  # in Docker there is no browser to open: the page shows the link instead
    proc = subprocess.Popen([exe, "auth", "login"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=tempfile.gettempdir())
    flow = {"proc": proc, "raw": "", "lock": threading.Lock(), "started": time.time()}
    threading.Thread(target=_reader, args=(flow,), daemon=True).start()
    flow_id = secrets.token_urlsafe(12)
    with _FLOWS_LOCK:
        _FLOWS[flow_id] = flow
    deadline = time.time() + 20
    url = ""
    while time.time() < deadline and not url and proc.poll() is None:
        time.sleep(0.2)
        with flow["lock"]:
            url = _login_url(flow["raw"])
    if not url:
        _end_flow(flow_id)
        raise RuntimeError("`claude auth login` no ha dado ninguna dirección de inicio de sesión.")
    return {"flow_id": flow_id, "url": url, "expires_in": 600}


def submit_login_code(flow_id: str, code: str) -> Dict[str, Any]:
    """Give the pasted code to the waiting CLI and report whether the account is now connected."""
    flow = _FLOWS.get(flow_id)
    if flow is None:
        return {"status": "unknown", "message": "Esa conexión ya no está en curso: empieza de nuevo."}
    code = re.sub(r"\s+", "", code or "")
    if not code:
        return {"status": "pending", "message": "Pega el código que te muestra Claude."}
    proc = flow["proc"]
    with flow["lock"]:
        seen = len(flow["raw"])
    try:
        proc.stdin.write((code + "\n").encode("utf-8"))
        proc.stdin.flush()
    except OSError:
        pass
    # The CLI either finishes (code accepted), exits with an error, or answers and waits for another code.
    deadline = time.time() + 40
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.3)
        with flow["lock"]:
            reply = _ANSI.sub("", flow["raw"][seen:]).strip()
        if reply and re.search(r"look like|invalid|expired|incorrect|try again|failed|error", reply, re.I):
            break
    status = auth_status()
    if status["logged_in"]:
        _end_flow(flow_id)
        return {"status": "authorized", "account": status["account"],
                "message": f"Conectado como {status['account']}" if status["account"] else "Claude conectado."}
    with flow["lock"]:
        reply = _ANSI.sub("", flow["raw"][seen:]).strip().splitlines()
    detail = next((line.strip() for line in reversed(reply) if line.strip() and "Paste code" not in line), "")
    if proc.poll() is not None:
        _end_flow(flow_id)  # the CLI gave up: a new attempt needs a new login
        return {"status": "error", "restart": True,
                "message": f"Claude no ha aceptado el código. {detail[:200]} Pulsa Cancelar y vuelve a empezar.".strip()}
    return {"status": "retry", "message": f"{detail[:200] or 'Código no válido.'} Cópialo entero, incluido el # del medio."}


def poll_login(flow_id: str) -> Dict[str, Any]:
    """The CLI can also finish by itself (sign-in on the same computer): report that too."""
    flow = _FLOWS.get(flow_id)
    if flow is None:
        return {"status": "unknown", "message": "Esa conexión ya no está en curso."}
    if time.time() - flow["started"] > 600:
        _end_flow(flow_id)
        return {"status": "error", "message": "El inicio de sesión ha caducado. Empieza de nuevo."}
    if flow["proc"].poll() is not None and auth_status()["logged_in"]:
        _end_flow(flow_id)
        return {"status": "authorized", "message": "Claude conectado."}
    return {"status": "pending", "message": ""}


def logout() -> bool:
    """Close Poda AI's own Claude session (the computer's own Claude Code is not touched)."""
    exe = executable()
    if not exe:
        return False
    try:
        subprocess.run([exe, "auth", "logout"], capture_output=True, text=True, timeout=30, env=_env(),
                       cwd=tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# ── models the account can use ────────────────────────────────────────────────────────────

def _pretty(model: str) -> str:
    match = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", model)
    if not match:
        return model
    family, major, minor = match.groups()
    return f"Claude {family.capitalize()} {major}" + (f".{minor}" if minor else "")


def _probe(exe: str, model: str) -> Optional[Dict[str, str]]:
    try:
        done = subprocess.run(
            [exe, "-p", "--output-format", "json", "--model", model, "--tools", "", "--no-session-persistence",
             "--setting-sources", "", "--strict-mcp-config", "--system-prompt", "Reply with the single word: ok"],
            input="ok", capture_output=True, text=True, timeout=90, env=_env(), cwd=tempfile.gettempdir())
        data = json.loads(done.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    # An unknown or unavailable model errors; an old id can also be silently served by a newer model, so the
    # model that really answered must be the one asked for.
    if data.get("is_error") or model not in (data.get("modelUsage") or {}):
        return None
    return {"id": model, "label": f"{_pretty(model)} · {model}"}


def available_models() -> List[Dict[str, str]]:
    exe = executable()
    if not exe:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(lambda m: _probe(exe, m), CANDIDATES))
    return [m for m in found if m]


# ── transport (LiteLLM-compatible) ────────────────────────────────────────────────────────

def _prompt(messages: List[Dict[str, Any]]):
    system: List[str] = []
    parts: List[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        content = str(content or "")
        role = message.get("role")
        if role == "system":
            system.append(content)
        elif role == "assistant":
            parts.append(f"[Previous assistant answer]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(system), "\n\n".join(parts)


def _classify(text: str) -> Exception:
    low = text.lower()
    if "not logged in" in low or "/login" in low or "authentication" in low or "invalid api key" in low:
        return AuthenticationError("Claude no tiene sesión iniciada: conéctala en el dashboard.")
    if any(marker in low for marker in _LIMIT_MARKERS):
        return BudgetExceededError(f"Límite de uso de tu plan de Claude alcanzado: {text[:300]}")
    return RuntimeError(text[:600] or "El CLI de Claude falló sin mensaje.")


async def acompletion(**kwargs: Any):
    global _semaphore
    exe = executable()
    if not exe:
        raise AuthenticationError("No se encuentra el comando `claude`: instala Claude Code.")
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_CONCURRENCY)
    model = str(kwargs.get("model") or CANDIDATES[0])
    model = model[len(PREFIX):] if model.startswith(PREFIX) else model
    system, prompt = _prompt(kwargs.get("messages") or [])
    command = [exe, "-p", "--output-format", "json", "--model", model, "--tools", "",
               "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"]
    if system:
        command += ["--system-prompt", system]
    schema = ((kwargs.get("response_format") or {}).get("json_schema") or {}).get("schema")
    if schema:
        command += ["--json-schema", json.dumps(schema)]
    timeout = float(kwargs.get("timeout") or 600)
    async with _semaphore:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_env(), cwd=tempfile.gettempdir())
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"El CLI de Claude no respondió en {int(timeout)} s.") from None
    text = out.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise _classify((err.decode("utf-8", errors="replace") or text)) from None
    if data.get("is_error") or proc.returncode:
        raise _classify(str(data.get("result") or err.decode("utf-8", errors="replace")))
    content = str(data.get("result") or "")
    if not content and data.get("structured_output") is not None:
        content = json.dumps(data["structured_output"], ensure_ascii=False)
    usage = data.get("usage") or {}
    cached = int(usage.get("cache_read_input_tokens") or 0)
    prompt_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) + cached
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=int(usage.get("output_tokens") or 0),
                              prompt_tokens_details=SimpleNamespace(cached_tokens=cached)),
        _hidden_params={})
