#!/usr/bin/env python3
# =============================================================================
# copilot_proxy.py — Proveedor GitHub Copilot para Poda AI (solo stdlib).
#
#   python -m poda.providers.copilot_proxy <subcomando>
#   (poda login copilot / poda models --provider copilot lo invocan solos)
#
# Subcomandos:
#   login              Inicia sesión con la cuenta de GitHub (device flow OAuth).
#   status             Comprueba la sesión y la suscripción de Copilot.
#   logout             Borra el token guardado.
#   models [--ids|--json|--all]
#                      Lista los modelos de chat disponibles para la cuenta.
#   serve [--port N]   Proxy local: recibe la API de Anthropic (/v1/messages)
#                      que usa Poda AI (LiteLLM, ruta anthropic/) y la traduce a la API de Copilot,
#                      con herramientas y streaming.
#
# Rutas de Copilot: cada modelo declara en `supported_endpoints` por dónde se
# le puede llamar. El proxy elige, por este orden de preferencia:
#   /chat/completions  (la mayoría de modelos)
#   /responses         (modelos que solo admiten la API Responses de OpenAI)
#   /v1/messages       (paso directo, solo si el modelo únicamente declara esta)
# Si Copilot responde que el modelo no está disponible por la ruta elegida, se
# prueba la siguiente y se recuerda la que funcionó. COPILOT_ENDPOINT
# (auto|chat|responses|messages) fuerza una ruta concreta sin alternativas.
#
# Datos en COPILOT_HOME (por defecto <carpeta de Poda AI>/.copilot, fuera de git):
#   github-token   Token OAuth de GitHub (nunca se imprime).
#   proxy-key      Clave local que exige el proxy (Poda AI la envía como api_key).
#
# El token de Copilot (≈30 min) se obtiene a partir del de GitHub y se renueva
# automáticamente antes de caducar.
# =============================================================================

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # same home as the rest of Poda AI (.env, .copilot/)
    from poda.config import PROJECT_ROOT as _PROJECT_ROOT
    ROOT = str(_PROJECT_ROOT)
except ImportError:  # run as a standalone script
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_home = os.environ.get("COPILOT_HOME") or ".copilot"
HOME = _home if os.path.isabs(_home) else os.path.join(ROOT, _home)
GITHUB_TOKEN_FILE = os.path.join(HOME, "github-token")
PROXY_KEY_FILE = os.path.join(HOME, "proxy-key")

CLIENT_ID = os.environ.get("COPILOT_CLIENT_ID", "Iv1.b507a08c87ecfe98")
GITHUB_WEB = "https://github.com"
GITHUB_API = os.environ.get("COPILOT_GITHUB_API", "https://api.github.com")
DEFAULT_COPILOT_API = "https://api.githubcopilot.com"
EDITOR_VERSION = os.environ.get("COPILOT_EDITOR_VERSION", "vscode/1.99.0")
PLUGIN_VERSION = os.environ.get("COPILOT_PLUGIN_VERSION", "copilot-chat/0.26.0")
USER_AGENT = os.environ.get("COPILOT_USER_AGENT", "GitHubCopilotChat/0.26.0")
UPSTREAM_TIMEOUT = int(os.environ.get("COPILOT_UPSTREAM_TIMEOUT", "600"))

# Rutas de Copilot (ver cabecera). El orden de ROUTE_ORDER es la preferencia.
ROUTE_CHAT, ROUTE_RESPONSES, ROUTE_MESSAGES = "chat", "responses", "messages"
ROUTE_PATHS = {ROUTE_CHAT: "/chat/completions", ROUTE_RESPONSES: "/responses", ROUTE_MESSAGES: "/v1/messages"}
ROUTE_ORDER = (ROUTE_CHAT, ROUTE_RESPONSES, ROUTE_MESSAGES)
ENDPOINT_OVERRIDE = (os.environ.get("COPILOT_ENDPOINT") or "auto").strip().lower()
REASONING_EFFORT = (os.environ.get("COPILOT_REASONING_EFFORT") or "").strip().lower()


class CopilotError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# ── Utilidades HTTP ─────────────────────────────────────────────────────────

def http_request(method, url, headers=None, body=None, timeout=30):
    """Devuelve (status, bytes). No lanza en errores HTTP."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except urllib.error.URLError as e:
        raise CopilotError(502, f"No se pudo conectar con {urllib.parse.urlsplit(url).netloc}: {e.reason}")


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return None


def upstream_message(raw, default):
    obj = parse_json(raw)
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if obj.get("message"):
            return str(obj["message"])
    text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).strip()
    return text[:500] or default


def upstream_code(raw):
    obj = parse_json(raw)
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])
        if obj.get("code"):
            return str(obj["code"])
    return None


# ── Almacenamiento ──────────────────────────────────────────────────────────

def write_secret(path, value):
    os.makedirs(HOME, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_secret(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def ensure_proxy_key():
    key = read_secret(PROXY_KEY_FILE)
    if not key:
        key = "cp-local-" + secrets.token_hex(24)
        write_secret(PROXY_KEY_FILE, key)
    return key


# ── Sesión de Copilot ───────────────────────────────────────────────────────

class CopilotSession:
    """Intercambia el token de GitHub por el de Copilot y lo renueva."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0
        self.api_base = DEFAULT_COPILOT_API
        self.info = {}

    def _exchange(self):
        gh = read_secret(GITHUB_TOKEN_FILE)
        if not gh:
            raise CopilotError(401, "No hay sesión de GitHub Copilot. Ejecuta: poda login copilot")
        status, raw = http_request("GET", f"{GITHUB_API}/copilot_internal/v2/token", headers={
            "Authorization": f"token {gh}",
            "Accept": "application/json",
            "Editor-Version": EDITOR_VERSION,
            "Editor-Plugin-Version": PLUGIN_VERSION,
            "User-Agent": USER_AGENT,
        })
        if status == 401:
            raise CopilotError(401, "El token de GitHub no es válido o se ha revocado. Ejecuta: poda login copilot")
        if status in (403, 404):
            raise CopilotError(403, "La cuenta no tiene una suscripción de GitHub Copilot activa o la organización no permite este acceso ("
                               + upstream_message(raw, f"HTTP {status}") + ")")
        if status != 200:
            raise CopilotError(502, f"Error al obtener el token de Copilot (HTTP {status}): " + upstream_message(raw, ""))
        data = parse_json(raw) or {}
        if not data.get("token"):
            raise CopilotError(502, "Respuesta inesperada al obtener el token de Copilot.")
        self._token = data["token"]
        self._expires_at = int(data.get("expires_at") or (time.time() + 1500))
        api = ((data.get("endpoints") or {}).get("api") or DEFAULT_COPILOT_API).rstrip("/")
        self.api_base = api
        self.info = {k: data.get(k) for k in ("sku", "chat_enabled", "expires_at") if k in data}

    def token(self, force=False):
        with self._lock:
            if force or not self._token or time.time() > self._expires_at - 120:
                self._exchange()
            return self._token

    def headers(self, force=False):
        return {
            "Authorization": f"Bearer {self.token(force)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Editor-Version": EDITOR_VERSION,
            "Editor-Plugin-Version": PLUGIN_VERSION,
            "Copilot-Integration-Id": "vscode-chat",
            "Openai-Intent": "conversation-panel",
            "User-Agent": USER_AGENT,
            "X-Request-Id": str(uuid.uuid4()),
        }

    def fetch_models(self):
        headers = self.headers()  # primero: el intercambio fija api_base
        status, raw = http_request("GET", f"{self.api_base}/models", headers=headers)
        if status == 401:
            headers = self.headers(force=True)
            status, raw = http_request("GET", f"{self.api_base}/models", headers=headers)
        if status != 200:
            raise CopilotError(status, "No se pudo obtener la lista de modelos: " + upstream_message(raw, f"HTTP {status}"))
        return (parse_json(raw) or {}).get("data") or []


def is_chat_model(m):
    caps = m.get("capabilities") or {}
    if caps.get("type") != "chat":
        return False
    if m.get("model_picker_enabled") is False:
        return False
    if (m.get("policy") or {}).get("state") == "disabled":
        return False
    return True


def model_limits(m):
    caps = m.get("capabilities") or {}
    limits = caps.get("limits") or {}
    supports = caps.get("supports") or {}
    return {
        "context": limits.get("max_context_window_tokens") or limits.get("max_prompt_tokens"),
        "max_output": limits.get("max_output_tokens"),
        "tools": bool(supports.get("tool_calls")),
        "streaming": supports.get("streaming", True) is not False,
    }


def model_endpoints(m):
    """Rutas que Copilot declara para el modelo (vacío si no las declara)."""
    eps = (m or {}).get("supported_endpoints")
    if not isinstance(eps, list):
        return []
    return ["/" + str(e).strip().strip("/") for e in eps if e]


def preferred_routes(m):
    """Rutas a probar, en orden: primero las declaradas por el modelo según la
    preferencia de ROUTE_ORDER; después chat y responses como alternativa por
    si la lista de modelos está desactualizada o no declara rutas."""
    eps = model_endpoints(m)
    declared = [r for r in ROUTE_ORDER if ROUTE_PATHS[r] in eps]
    return declared + [r for r in (ROUTE_CHAT, ROUTE_RESPONSES) if r not in declared]


def endpoint_unsupported(err):
    """¿El error de Copilot indica que el modelo no admite esa ruta?"""
    if err.status not in (400, 404, 405, 415, 422):
        return False
    if (err.code or "").lower() in ("unsupported_api_for_model", "unsupported_endpoint"):
        return True
    msg = (err.message or "").lower()
    return "endpoint" in msg and any(w in msg for w in ("not accessible", "not supported", "unsupported", "not available"))


# ── CLI: login / status / logout / models ───────────────────────────────────

def cmd_login(_args):
    status, raw = http_request("POST", f"{GITHUB_WEB}/login/device/code",
                               headers={"Accept": "application/json", "Content-Type": "application/json",
                                        "User-Agent": USER_AGENT},
                               body={"client_id": CLIENT_ID, "scope": "read:user"})
    data = parse_json(raw) or {}
    if status != 200 or not data.get("device_code"):
        print(f"✗ GitHub no ha iniciado el device flow (HTTP {status}): {upstream_message(raw, '')}", file=sys.stderr)
        return 1

    uri = data.get("verification_uri", "https://github.com/login/device")
    interval = int(data.get("interval") or 5)
    deadline = time.time() + int(data.get("expires_in") or 900)
    print()
    print("  Inicio de sesión en GitHub Copilot")
    print(f"  1. Abre en el navegador:  {uri}")
    print(f"  2. Introduce el código:   \033[1m{data['user_code']}\033[0m")
    print("  3. Autoriza el acceso. Esta ventana continuará sola.")
    print()

    while time.time() < deadline:
        time.sleep(interval)
        status, raw = http_request("POST", f"{GITHUB_WEB}/login/oauth/access_token",
                                   headers={"Accept": "application/json", "Content-Type": "application/json",
                                            "User-Agent": USER_AGENT},
                                   body={"client_id": CLIENT_ID, "device_code": data["device_code"],
                                         "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        res = parse_json(raw) or {}
        if res.get("access_token"):
            write_secret(GITHUB_TOKEN_FILE, res["access_token"])
            print("  ✔ Autorizado. Comprobando la suscripción de Copilot...")
            return cmd_status(_args)
        err = res.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(res.get("interval") or interval + 5)
            continue
        if err == "expired_token":
            break
        if err == "access_denied":
            print("✗ Has cancelado la autorización en GitHub.", file=sys.stderr)
            return 1
        print(f"✗ Error en el inicio de sesión: {res.get('error_description') or err or f'HTTP {status}'}", file=sys.stderr)
        return 1
    print("✗ El código ha caducado sin autorizarse. Vuelve a ejecutar el login.", file=sys.stderr)
    return 1


def cmd_status(_args):
    gh = read_secret(GITHUB_TOKEN_FILE)
    if not gh:
        print("✗ Sin sesión de GitHub Copilot. Ejecuta: poda login copilot", file=sys.stderr)
        return 1
    status, raw = http_request("GET", f"{GITHUB_API}/user", headers={
        "Authorization": f"token {gh}", "Accept": "application/json", "User-Agent": USER_AGENT})
    user = (parse_json(raw) or {}).get("login") if status == 200 else None
    session = CopilotSession()
    try:
        session.token()
    except CopilotError as e:
        print(f"✗ {e.message}", file=sys.stderr)
        return 2
    sku = session.info.get("sku")
    print(f"  ✔ GitHub Copilot activo{' · cuenta ' + user if user else ''}{' · plan ' + sku if sku else ''}")
    print(f"    API: {session.api_base}")
    return 0


def cmd_logout(_args):
    try:
        os.remove(GITHUB_TOKEN_FILE)
        print("  ✔ Sesión local eliminada. Para revocar la autorización en GitHub:")
        print("    https://github.com/settings/applications")
    except FileNotFoundError:
        print("  No había sesión guardada.")
    return 0


def cmd_models(args):
    session = CopilotSession()
    try:
        models = session.fetch_models()
    except CopilotError as e:
        print(f"✗ {e.message}", file=sys.stderr)
        return 2 if e.status in (401, 403) else 1
    if args.json:
        print(json.dumps(models, indent=2, ensure_ascii=False))
        return 0
    if not args.all:
        models = [m for m in models if is_chat_model(m)]
    seen, rows = set(), []
    for m in models:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        rows.append(m)
    rows.sort(key=lambda m: m["id"].lower())
    if args.ids:
        for m in rows:
            print(m["id"])
        return 0
    if args.describe:
        # id<TAB>descripción corta (para el selector)
        for m in rows:
            lim = model_limits(m)
            parts = [m.get("vendor") or ""]
            if lim["context"]:
                parts.append(f"{int(lim['context']) // 1000}k ctx")
            if not lim["tools"]:
                parts.append("sin tools")
            route = preferred_routes(m)[0]
            if route != ROUTE_CHAT:
                parts.append(f"vía {ROUTE_PATHS[route]}")
            if (m.get("policy") or {}).get("state") not in (None, "enabled"):
                parts.append("requiere activarlo en GitHub")
            print(f"{m['id']}\t{' · '.join(p for p in parts if p)}")
        return 0
    print(f"{'MODELO':38} {'PROVEEDOR':12} {'CONTEXTO':>9} {'SALIDA':>7} {'TOOLS':5} RUTA")
    for m in rows:
        lim = model_limits(m)
        print(f"{m['id']:38} {(m.get('vendor') or '-'):12} {str(lim['context'] or '-'):>9} "
              f"{str(lim['max_output'] or '-'):>7} {('sí' if lim['tools'] else 'no'):5} "
              f"{ROUTE_PATHS[preferred_routes(m)[0]]}")
    return 0


# ── Traducción Anthropic Messages → OpenAI Chat Completions ─────────────────

def _blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)]


def _image_part(block):
    src = block.get("source") or {}
    if src.get("type") == "base64":
        url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    else:
        url = src.get("url", "")
    return {"type": "image_url", "image_url": {"url": url}}


def _join_text(blocks):
    return "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text"))


def _clean_schema(schema):
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    s = dict(schema)
    s.pop("$schema", None)
    s.setdefault("type", "object")
    if s.get("type") == "object":
        s.setdefault("properties", {})
    return s


def anthropic_to_openai(body, model_id, max_output_cap=None):
    messages = []
    system = body.get("system")
    if system:
        text = system if isinstance(system, str) else _join_text(_blocks(system))
        if text:
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        role = msg.get("role")
        blocks = _blocks(msg.get("content"))
        if role == "assistant":
            text = _join_text(blocks)
            tool_calls = [{
                "id": b.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
            } for b in blocks if b.get("type") == "tool_use"]
            out = {"role": "assistant", "content": text or None}
            if tool_calls:
                out["tool_calls"] = tool_calls
            elif not text:
                out["content"] = ""
            messages.append(out)
            continue

        # Usuario: primero los resultados de herramientas, luego el resto.
        pending_images = []
        for b in blocks:
            if b.get("type") != "tool_result":
                continue
            inner = _blocks(b.get("content"))
            text = _join_text(inner)
            pending_images += [_image_part(x) for x in inner if x.get("type") == "image"]
            if b.get("is_error"):
                text = "Error: " + (text or "la herramienta falló")
            messages.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": text or "(sin salida)"})

        rest = [b for b in blocks if b.get("type") in ("text", "image")]
        if pending_images:
            rest = [{"type": "text", "text": "Imágenes devueltas por la herramienta:"}] + \
                   [{"type": "_image", "part": p} for p in pending_images] + rest
        if not rest:
            continue
        if any(b.get("type") in ("image", "_image") for b in rest):
            parts = []
            for b in rest:
                if b.get("type") == "text" and b.get("text"):
                    parts.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image":
                    parts.append(_image_part(b))
                elif b.get("type") == "_image":
                    parts.append(b["part"])
            messages.append({"role": "user", "content": parts})
        else:
            text = _join_text(rest)
            if text:
                messages.append({"role": "user", "content": text})

    req = {"model": model_id, "messages": messages}

    max_tokens = body.get("max_tokens")
    if max_output_cap:
        max_tokens = min(int(max_tokens or max_output_cap), int(max_output_cap))
    if max_tokens:
        req["max_tokens"] = int(max_tokens)
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            req[dst] = body[src]
    if body.get("stop_sequences"):
        req["stop"] = body["stop_sequences"]

    tools = body.get("tools") or []
    fn_tools = [{
        "type": "function",
        "function": {"name": t.get("name", ""), "description": t.get("description", ""),
                     "parameters": _clean_schema(t.get("input_schema"))},
    } for t in tools if t.get("name") and t.get("type") in (None, "custom")]
    if fn_tools:
        req["tools"] = fn_tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            req["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            req["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif kind == "none":
            req["tool_choice"] = "none"
        elif kind == "auto":
            req["tool_choice"] = "auto"

    if body.get("stream"):
        req["stream"] = True
        req["stream_options"] = {"include_usage": True}
    return req


STOP_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
            "function_call": "tool_use", "content_filter": "end_turn"}


def _usage(u):
    u = u or {}
    prompt = int(u.get("prompt_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return {"input_tokens": max(prompt - cached, 0), "output_tokens": int(u.get("completion_tokens") or 0),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


def _parse_args(raw):
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except ValueError:
        return {"_raw": raw}


def openai_to_anthropic(resp, model_name):
    content, stop = [], None
    # Copilot puede repartir la respuesta (texto / tool_calls) en varias choices.
    for ch in resp.get("choices") or []:
        msg = ch.get("message") or {}
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            content.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": fn.get("name", ""), "input": _parse_args(fn.get("arguments"))})
        stop = STOP_MAP.get(ch.get("finish_reason"), stop or "end_turn")
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    return {"id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": model_name,
            "content": content, "stop_reason": stop or "end_turn", "stop_sequence": None,
            "usage": _usage(resp.get("usage"))}


class StreamTranslator:
    """Convierte chunks SSE de OpenAI en eventos SSE de Anthropic."""

    def __init__(self, model_name):
        self.model = model_name
        self.index = -1
        self.open = None          # "text" | ("tool", key) | None
        self.tools = {}           # (choice, idx) -> (block_index, id)
        self.stop = None
        self.usage = None
        self.saw_tool = False

    @staticmethod
    def event(name, data):
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

    def start(self):
        return self.event("message_start", {"type": "message_start", "message": {
            "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close(self):
        if self.open is None:
            return []
        self.open = None
        return [self.event("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def feed(self, chunk):
        out = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            ci = ch.get("index", 0)
            delta = ch.get("delta") or {}
            text = delta.get("content")
            if text:
                if self.open != "text":
                    out += self._close()
                    self.index += 1
                    self.open = "text"
                    out.append(self.event("content_block_start", {"type": "content_block_start", "index": self.index,
                                                                  "content_block": {"type": "text", "text": ""}}))
                out.append(self.event("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                              "delta": {"type": "text_delta", "text": text}}))
            for tc in delta.get("tool_calls") or []:
                key = (ci, tc.get("index", 0))
                fn = tc.get("function") or {}
                known = self.tools.get(key)
                if known is None or (tc.get("id") and tc["id"] != known[1]):
                    out += self._close()
                    self.index += 1
                    tid = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                    self.tools[key] = (self.index, tid)
                    self.open = ("tool", key)
                    self.saw_tool = True
                    out.append(self.event("content_block_start", {"type": "content_block_start", "index": self.index,
                                                                  "content_block": {"type": "tool_use", "id": tid,
                                                                                    "name": fn.get("name", ""), "input": {}}}))
                args = fn.get("arguments")
                if args:
                    out.append(self.event("content_block_delta", {"type": "content_block_delta",
                                                                  "index": self.tools[key][0],
                                                                  "delta": {"type": "input_json_delta", "partial_json": args}}))
            if ch.get("finish_reason"):
                self.stop = STOP_MAP.get(ch["finish_reason"], "end_turn")
        return out

    def finish(self):
        out = self._close()
        stop = "tool_use" if self.saw_tool else (self.stop or "end_turn")
        out.append(self.event("message_delta", {"type": "message_delta",
                                                "delta": {"stop_reason": stop, "stop_sequence": None},
                                                "usage": _usage(self.usage)}))
        out.append(self.event("message_stop", {"type": "message_stop"}))
        return out

    def anthropic_usage(self):
        return _usage(self.usage)


# ── Traducción Anthropic Messages → OpenAI Responses ────────────────────────
# Para los modelos que Copilot solo sirve por /responses. La API es "stateless"
# aquí (store=false): cada petición lleva el historial completo, igual que con
# chat/completions.

def _input_image(block):
    return {"type": "input_image", "image_url": _image_part(block)["image_url"]["url"], "detail": "auto"}


def anthropic_to_responses(body, model_id, max_output_cap=None):
    items = []
    system = body.get("system")
    instructions = None
    if system:
        instructions = system if isinstance(system, str) else _join_text(_blocks(system))

    for msg in body.get("messages") or []:
        role = msg.get("role")
        blocks = _blocks(msg.get("content"))
        if role == "assistant":
            text = _join_text(blocks)
            if text:
                items.append({"role": "assistant", "content": text})
            for b in blocks:
                if b.get("type") == "tool_use":
                    items.append({"type": "function_call",
                                  "call_id": b.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                                  "name": b.get("name", ""),
                                  "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)})
            continue

        # Usuario: primero los resultados de herramientas, luego el resto.
        pending_images = []
        for b in blocks:
            if b.get("type") != "tool_result":
                continue
            inner = _blocks(b.get("content"))
            text = _join_text(inner)
            pending_images += [_input_image(x) for x in inner if x.get("type") == "image"]
            if b.get("is_error"):
                text = "Error: " + (text or "la herramienta falló")
            items.append({"type": "function_call_output", "call_id": b.get("tool_use_id", ""),
                          "output": text or "(sin salida)"})

        parts = []
        if pending_images:
            parts.append({"type": "input_text", "text": "Imágenes devueltas por la herramienta:"})
            parts += pending_images
        for b in blocks:
            if b.get("type") == "text" and b.get("text"):
                parts.append({"type": "input_text", "text": b["text"]})
            elif b.get("type") == "image":
                parts.append(_input_image(b))
        if parts:
            items.append({"role": "user", "content": parts})

    req = {"model": model_id, "input": items, "store": False}
    if instructions:
        req["instructions"] = instructions

    max_tokens = body.get("max_tokens")
    if max_output_cap:
        max_tokens = min(int(max_tokens or max_output_cap), int(max_output_cap))
    if max_tokens:
        req["max_output_tokens"] = int(max_tokens)
    # temperature/top_p/stop no se envían: los modelos de razonamiento de la API
    # Responses los rechazan y Poda AI solo usa valores por defecto.
    if REASONING_EFFORT:
        req["reasoning"] = {"effort": REASONING_EFFORT}

    tools = body.get("tools") or []
    fn_tools = [{
        "type": "function", "name": t.get("name", ""), "description": t.get("description", ""),
        "parameters": _clean_schema(t.get("input_schema")),
        "strict": False,  # en Responses el modo estricto es el predeterminado y exige esquemas cerrados
    } for t in tools if t.get("name") and t.get("type") in (None, "custom")]
    if fn_tools:
        req["tools"] = fn_tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            req["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            req["tool_choice"] = {"type": "function", "name": tc["name"]}
        elif kind == "none":
            req["tool_choice"] = "none"
        elif kind == "auto":
            req["tool_choice"] = "auto"
        if tc.get("disable_parallel_tool_use"):
            req["parallel_tool_calls"] = False

    if body.get("stream"):
        req["stream"] = True
    return req


def _usage_responses(u):
    u = u or {}
    prompt = int(u.get("input_tokens") or 0)
    cached = int(((u.get("input_tokens_details") or {}).get("cached_tokens")) or 0)
    return {"input_tokens": max(prompt - cached, 0), "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


def _responses_stop(resp, saw_tool):
    if saw_tool:
        return "tool_use"
    if (resp or {}).get("status") == "incomplete":
        reason = ((resp or {}).get("incomplete_details") or {}).get("reason")
        return "max_tokens" if reason == "max_output_tokens" else "end_turn"
    return "end_turn"


def _responses_error(resp, default):
    err = (resp or {}).get("error") or {}
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return default


def responses_to_anthropic(resp, model_name):
    if resp.get("status") == "failed" or (resp.get("error") and not resp.get("output")):
        raise CopilotError(502, _responses_error(resp, "la respuesta de Copilot ha fallado"))
    content = []
    for item in resp.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                text = part.get("text") if part.get("type") in ("output_text", "text") else part.get("refusal")
                if text:
                    content.append({"type": "text", "text": text})
        elif kind == "function_call":
            content.append({"type": "tool_use",
                            "id": item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": item.get("name", ""), "input": _parse_args(item.get("arguments"))})
        # "reasoning" y otros items internos no se trasladan al cliente.
    saw_tool = any(b["type"] == "tool_use" for b in content)
    return {"id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": model_name,
            "content": content, "stop_reason": _responses_stop(resp, saw_tool), "stop_sequence": None,
            "usage": _usage_responses(resp.get("usage"))}


def _is_json(raw):
    try:
        json.loads(raw or "{}")
        return True
    except ValueError:
        return False


class ResponsesStreamTranslator(StreamTranslator):
    """Convierte los eventos SSE de la API Responses en eventos SSE de Anthropic.

    El texto se reenvía en cuanto llega. Cada llamada a herramienta se emite
    completa (inicio, argumentos, fin) cuando Copilot cierra su item: así los
    bloques nunca se solapan aunque se intercalen argumentos de varias llamadas.
    """

    def __init__(self, model_name):
        super().__init__(model_name)
        self.text_key = None
        self.calls = {}      # clave del item -> {"call_id", "name", "args", "emitted"}
        self.order = []
        self.final = None    # objeto "response" de response.completed / incomplete

    def _call(self, ev, item=None):
        item = item or {}
        key = ev.get("output_index")
        if key is None:
            key = item.get("id") or ev.get("item_id")
        c = self.calls.get(key)
        if c is None:
            c = {"call_id": None, "name": "", "args": "", "emitted": False}
            self.calls[key] = c
            self.order.append(key)
        if item.get("call_id"):
            c["call_id"] = item["call_id"]
        if item.get("name"):
            c["name"] = item["name"]
        return c

    def _emit_call(self, c):
        if c["emitted"]:
            return []
        c["emitted"] = True
        out = self._close()
        self.index += 1
        self.saw_tool = True
        tid = c["call_id"] or f"toolu_{uuid.uuid4().hex[:24]}"
        out.append(self.event("content_block_start", {"type": "content_block_start", "index": self.index,
                                                      "content_block": {"type": "tool_use", "id": tid,
                                                                        "name": c["name"], "input": {}}}))
        if c["args"]:
            out.append(self.event("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                          "delta": {"type": "input_json_delta",
                                                                    "partial_json": c["args"]}}))
        out.append(self.event("content_block_stop", {"type": "content_block_stop", "index": self.index}))
        return out

    def feed(self, ev):
        out = []
        kind = ev.get("type") or ""
        if kind in ("response.output_text.delta", "response.refusal.delta"):
            text = ev.get("delta") or ""
            if text:
                key = (ev.get("output_index"), ev.get("content_index"))
                if self.open != "text" or self.text_key != key:
                    out += self._close()
                    self.index += 1
                    self.open, self.text_key = "text", key
                    out.append(self.event("content_block_start", {"type": "content_block_start", "index": self.index,
                                                                  "content_block": {"type": "text", "text": ""}}))
                out.append(self.event("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                              "delta": {"type": "text_delta", "text": text}}))
        elif kind == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                self._call(ev, item)
        elif kind == "response.function_call_arguments.delta":
            self._call(ev)["args"] += ev.get("delta") or ""
        elif kind == "response.function_call_arguments.done":
            if ev.get("arguments") is not None:
                self._call(ev)["args"] = ev["arguments"]
        elif kind == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                c = self._call(ev, item)
                if item.get("arguments") is not None:
                    c["args"] = item["arguments"]
                out += self._emit_call(c)
            elif item.get("type") == "message" and self.open == "text":
                out += self._close()
        elif kind in ("response.completed", "response.incomplete"):
            self.final = ev.get("response") or {}
            if self.final.get("usage"):
                self.usage = self.final["usage"]
        elif kind == "response.failed":
            raise CopilotError(502, _responses_error(ev.get("response"), "la respuesta de Copilot ha fallado"))
        elif kind == "error":
            raise CopilotError(502, str(ev.get("message") or ev.get("code") or "error en el stream"))
        # Resto de eventos (created, in_progress, reasoning, content_part...) sin efecto.
        return out

    def finish(self):
        out = self._close()
        # Llamadas que no llegaron a cerrarse (p. ej. corte por max_output_tokens):
        # solo se emiten si sus argumentos son JSON completo.
        for key in self.order:
            c = self.calls[key]
            if not c["emitted"] and c["name"] and _is_json(c["args"]):
                out += self._emit_call(c)
        stop = "tool_use" if self.saw_tool else _responses_stop(self.final, False)
        out.append(self.event("message_delta", {"type": "message_delta",
                                                "delta": {"stop_reason": stop, "stop_sequence": None},
                                                "usage": _usage_responses(self.usage)}))
        out.append(self.event("message_stop", {"type": "message_stop"}))
        return out

    def anthropic_usage(self):
        return _usage_responses(self.usage)


# ── Paso directo Anthropic Messages → Copilot /v1/messages ──────────────────
# Solo para modelos que únicamente declaran /v1/messages: el cuerpo ya está en
# el formato correcto y la respuesta se reenvía tal cual.

def anthropic_passthrough(body, model_id, max_output_cap=None):
    req = dict(body)
    req["model"] = model_id
    if max_output_cap:
        req["max_tokens"] = min(int(req.get("max_tokens") or max_output_cap), int(max_output_cap))
    return req


BUILDERS = {ROUTE_CHAT: anthropic_to_openai, ROUTE_RESPONSES: anthropic_to_responses,
            ROUTE_MESSAGES: anthropic_passthrough}


# ── Proxy HTTP ──────────────────────────────────────────────────────────────

ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error"}


def log(msg):
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


class ProxyState:
    def __init__(self, key):
        self.key = key
        self.session = CopilotSession()
        self._models = None
        self._models_at = 0
        self._lock = threading.Lock()
        self._routes = {}    # modelo -> ruta que ya ha funcionado

    def routes(self, model_id, info):
        if ENDPOINT_OVERRIDE in ROUTE_PATHS:
            return [ENDPOINT_OVERRIDE]
        order = preferred_routes(info)
        known = self._routes.get(model_id)
        if known:
            order = [known] + [r for r in order if r != known]
        return order

    def remember_route(self, model_id, route):
        self._routes[model_id] = route

    def models(self):
        with self._lock:
            if self._models is None or time.time() - self._models_at > 1800:
                try:
                    self._models = {m["id"]: m for m in self.session.fetch_models() if m.get("id")}
                    self._models_at = time.time()
                except CopilotError as e:
                    log(f"aviso: no se pudo leer la lista de modelos ({e.message})")
                    self._models = self._models or {}
            return self._models


def make_handler(state):
    env_cap = os.environ.get("COPILOT_MAX_OUTPUT_TOKENS")
    env_cap = int(env_cap) if env_cap and env_cap.isdigit() else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "poda-copilot-proxy"

        def log_message(self, fmt, *args):  # silencia el log por defecto
            pass

        # ── utilidades de respuesta ──
        def _send_json(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, status, message):
            etype = ERROR_TYPES.get(status, "api_error" if status >= 500 else "invalid_request_error")
            self._send_json(status, {"type": "error", "error": {"type": etype, "message": message}})

        def _chunk(self, data):
            if data:
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

        def _authorized(self):
            supplied = self.headers.get("x-api-key") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                supplied = supplied or auth[7:].strip()
            return bool(supplied) and secrets.compare_digest(supplied, state.key)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return parse_json(raw) if raw else {}

        # ── rutas ──
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            if path == "/health":
                return self._send_json(200, {"status": "ok", "logged_in": bool(read_secret(GITHUB_TOKEN_FILE))})
            if not self._authorized():
                return self._send_error(401, "Clave del proxy local no válida.")
            if path == "/v1/models":
                data = [{"type": "model", "id": mid, "display_name": m.get("name") or mid, "created_at": None}
                        for mid, m in sorted(state.models().items()) if is_chat_model(m)]
                return self._send_json(200, {"data": data, "has_more": False, "first_id": None, "last_id": None})
            return self._send_error(404, f"Ruta no soportada: {path}")

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            if not self._authorized():
                return self._send_error(401, "Clave del proxy local no válida.")
            body = self._read_body()
            if not isinstance(body, dict):
                return self._send_error(400, "El cuerpo de la petición no es JSON válido.")
            if path == "/v1/messages/count_tokens":
                approx = len(json.dumps(body, ensure_ascii=False)) // 4
                return self._send_json(200, {"input_tokens": approx})
            if path == "/v1/messages":
                return self._messages(body)
            if path == "/shutdown":
                # poda dashboard → "Desconectar": forget the Copilot token held in memory.
                self._send_json(200, {"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
            return self._send_error(404, f"Ruta no soportada: {path}")

        def _messages(self, body):
            started = time.time()
            requested = str(body.get("model") or os.environ.get("COPILOT_MODEL") or "")
            model_id = requested.split("/", 1)[1] if requested.startswith("copilot/") else requested
            if not model_id:
                return self._send_error(400, "Falta el modelo (COPILOT_MODEL).")
            info = state.models().get(model_id)
            cap = env_cap
            if info:
                limit = model_limits(info)["max_output"]
                if limit:
                    cap = min(cap, int(limit)) if cap else int(limit)
            stream = bool(body.get("stream"))
            name = requested or model_id

            # Se prueba cada ruta posible del modelo; solo se pasa a la siguiente
            # si Copilot dice que el modelo no está disponible por esa ruta.
            resp = route = err = None
            tried = []
            for route in state.routes(model_id, info):
                tried.append(ROUTE_PATHS[route])
                try:
                    resp = self._upstream(BUILDERS[route](body, model_id, cap), route)
                    break
                except CopilotError as e:
                    wrong_route = endpoint_unsupported(e)
                    if err is None or not wrong_route:
                        err = e  # se informa del primer error, o del primero que no sea de ruta
                    if ENDPOINT_OVERRIDE in ROUTE_PATHS or not wrong_route:
                        break
                    log(f"{model_id}: Copilot no lo sirve por {ROUTE_PATHS[route]}; se prueba otra ruta")
            if resp is None:
                e = err or CopilotError(502, "no hay rutas disponibles para el modelo")
                msg = e.message
                if len(tried) > 1 and endpoint_unsupported(e):
                    msg += f" (rutas probadas: {', '.join(tried)})"
                log(f"{model_id} stream={stream} -> {e.status} {msg[:300]}")
                return self._send_error(e.status, f"GitHub Copilot: {msg}")
            state.remember_route(model_id, route)

            try:
                if route == ROUTE_MESSAGES:
                    usage = self._relay_passthrough(resp, stream)
                elif stream:
                    tr = ResponsesStreamTranslator(name) if route == ROUTE_RESPONSES else StreamTranslator(name)
                    usage = self._relay_stream(resp, tr)
                else:
                    data = parse_json(resp.read()) or {}
                    try:
                        if route == ROUTE_RESPONSES:
                            out = responses_to_anthropic(data, name)
                        else:
                            out = openai_to_anthropic(data, name)
                    except CopilotError as e:
                        log(f"{model_id} {ROUTE_PATHS[route]} stream=False -> {e.status} {e.message[:200]}")
                        return self._send_error(e.status, f"GitHub Copilot: {e.message}")
                    usage = out["usage"]
                    self._send_json(200, out)
            except (BrokenPipeError, ConnectionResetError):
                log(f"{model_id} cliente desconectado")
                return
            finally:
                resp.close()
            log(f"{model_id} {ROUTE_PATHS[route]} stream={stream} -> 200 {int((time.time() - started) * 1000)}ms "
                f"in={usage.get('input_tokens', 0)}+{usage.get('cache_read_input_tokens', 0)}c "
                f"out={usage.get('output_tokens', 0)}")

        def _upstream(self, req, route=ROUTE_CHAT):
            data = json.dumps(req, ensure_ascii=False).encode("utf-8")
            for attempt in (0, 1):
                headers = state.session.headers(force=attempt == 1)
                if req.get("stream"):
                    headers["Accept"] = "text/event-stream"
                if route == ROUTE_MESSAGES:
                    headers["anthropic-version"] = self.headers.get("anthropic-version") or "2023-06-01"
                r = urllib.request.Request(f"{state.session.api_base}{ROUTE_PATHS[route]}", data=data,
                                           method="POST", headers=headers)
                try:
                    return urllib.request.urlopen(r, timeout=UPSTREAM_TIMEOUT)
                except urllib.error.HTTPError as e:
                    raw = e.read() or b""
                    if e.code == 401 and attempt == 0:
                        continue  # token caducado: renovar y reintentar una vez
                    msg = upstream_message(raw, f"HTTP {e.code}")
                    if e.code == 429:
                        msg = "límite de uso alcanzado (rate limit o cuota de premium requests): " + msg
                    elif e.code == 403:
                        msg = "acceso denegado (¿modelo no habilitado en tu plan o política de la organización?): " + msg
                    raise CopilotError(e.code, msg, upstream_code(raw))
                except urllib.error.URLError as e:
                    raise CopilotError(502, f"no se pudo conectar con {state.session.api_base}: {e.reason}")
            raise CopilotError(401, "Copilot rechaza el token incluso tras renovarlo. Ejecuta: poda login copilot")

        def _relay_passthrough(self, resp, stream):
            """Reenvía la respuesta de Copilot /v1/messages sin traducirla."""
            usage = {}
            if not stream:
                raw = resp.read()
                usage = (parse_json(raw) or {}).get("usage") or {}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return usage
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self._chunk(line)
                    if line.startswith(b"data:"):
                        ev = parse_json(line[5:].strip()) or {}
                        u = ((ev.get("message") or {}).get("usage") if ev.get("type") == "message_start"
                             else ev.get("usage"))
                        if isinstance(u, dict):
                            usage.update({k: v for k, v in u.items() if isinstance(v, int)})
            except (OSError, ValueError) as e:
                if isinstance(e, (BrokenPipeError, ConnectionResetError)):
                    raise
                self._chunk(StreamTranslator.event("error", {"type": "error", "error": {
                    "type": "api_error", "message": f"GitHub Copilot: stream interrumpido ({e})"}}))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return usage

        def _relay_stream(self, resp, tr):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(tr.start())
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        break
                    chunk = parse_json(payload)
                    if isinstance(chunk, dict):
                        if chunk.get("error"):
                            raise CopilotError(502, upstream_message(payload, "error en el stream"))
                        for ev in tr.feed(chunk):
                            self._chunk(ev)
                for ev in tr.finish():
                    self._chunk(ev)
            except CopilotError as e:
                self._chunk(StreamTranslator.event("error", {"type": "error", "error": {
                    "type": "api_error", "message": f"GitHub Copilot: {e.message}"}}))
            except (OSError, ValueError) as e:
                if isinstance(e, (BrokenPipeError, ConnectionResetError)):
                    raise
                self._chunk(StreamTranslator.event("error", {"type": "error", "error": {
                    "type": "api_error", "message": f"GitHub Copilot: stream interrumpido ({e})"}}))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return tr.anthropic_usage()

    return Handler


class ProxyServer(ThreadingHTTPServer):
    """No imprime traceback cuando el cliente cierra una conexión (p. ej. una
    conexión keep-alive inactiva al terminar Poda AI); el resto de errores
    se siguen mostrando."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def cmd_serve(args):
    key = ensure_proxy_key()
    state = ProxyState(key)
    server = ProxyServer((args.host, args.port), make_handler(state))
    server.daemon_threads = True
    log(f"proxy Copilot escuchando en http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main():
    p = argparse.ArgumentParser(description="Proveedor GitHub Copilot para Poda AI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("logout").set_defaults(fn=cmd_logout)
    m = sub.add_parser("models")
    g = m.add_mutually_exclusive_group()
    g.add_argument("--ids", action="store_true", help="solo los identificadores")
    g.add_argument("--describe", action="store_true", help="id<TAB>descripción")
    g.add_argument("--json", action="store_true", help="respuesta completa de la API")
    m.add_argument("--all", action="store_true", help="sin filtrar (incluye embeddings y ocultos)")
    m.set_defaults(fn=cmd_models)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("COPILOT_PROXY_PORT") or 4141))
    s.set_defaults(fn=cmd_serve)
    args = p.parse_args()
    try:
        return args.fn(args)
    except CopilotError as e:
        print(f"✗ {e.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
