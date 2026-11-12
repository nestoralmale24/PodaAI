"""Local web dashboard to manage providers and the model Poda AI uses.

    poda dashboard            → http://127.0.0.1:8770/?t=<access token>

Standard library only. Protections for a local admin page:
* listens on 127.0.0.1 by default;
* every API call needs this run's session (HttpOnly SameSite=Strict cookie set by the page);
* the Host header must be a loopback name (blocks DNS rebinding);
* bodies are JSON only and small; no secret is ever sent to the browser.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .. import llm_settings as ls
from .. import sonar_setup
from . import runs

PAGE = Path(__file__).resolve().parent / "index.html"
ASSETS = Path(__file__).resolve().parent / "assets"
ASSET_TYPES = {".svg": "image/svg+xml", ".css": "text/css; charset=utf-8"}
MAX_BODY = 64 * 1024
SESSION_COOKIE = "poda_session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def effective_model() -> Dict[str, Any]:
    """What `poda run` would use now (without starting the Copilot proxy)."""
    from ..config import ConfigError, load_env, resolve_llm
    cfg = load_env()
    try:
        llm = resolve_llm(cfg, start_proxy=False)
    except ConfigError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "provider": llm.provider, "model": llm.model, "triage_model": llm.triage_model,
            "display": llm.display, "source": "dashboard" if cfg.get("PODA_SELECTION_SOURCE") else "env"}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], token: Optional[str] = None,
                 extra_hosts: Tuple[str, ...] = ()) -> None:
        super().__init__(address, Handler)
        self.token = token or secrets.token_urlsafe(24)
        self.allowed_hosts = LOOPBACK_HOSTS | {h.lower() for h in extra_hosts}

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"http://{shown}:{port}/"


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "poda-dashboard"
    sys_version = ""

    def log_message(self, *args: Any) -> None:  # keep the terminal clean
        pass

    # -- helpers ---------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in self.server.allowed_hosts

    def _session(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _token_ok(self) -> bool:
        """API calls need this run's session: the HttpOnly cookie set by the page (or the header)."""
        given = self.headers.get("X-Poda-Token") or self._session()
        return bool(given) and secrets.compare_digest(str(given), self.server.token)

    def _body(self) -> Optional[Dict[str, Any]]:
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # -- routing ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            self._send(HTTPStatus.MISDIRECTED_REQUEST, b"Host no permitido", "text/plain; charset=utf-8")
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if method == "GET" and parts.path in ("/", "/index.html"):
            # The page hands the browser this run's session in an HttpOnly, SameSite=Strict cookie:
            # nothing secret in the URL, and other sites can neither read it nor send it.
            cookie = f"{SESSION_COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", extra_headers={"Set-Cookie": cookie})
            return
        if method == "GET" and parts.path.startswith("/assets/"):
            # Brand assets (logos, tokens.css): public, no secrets, whitelisted names only.
            name = parts.path[len("/assets/"):]
            asset = ASSETS / name
            if "/" in name or asset.suffix not in ASSET_TYPES or not asset.is_file():
                self._json({"ok": False, "error": "No encontrado"}, 404)
                return
            self._send(200, asset.read_bytes(), ASSET_TYPES[asset.suffix])
            return
        if not parts.path.startswith("/api/"):
            self._json({"ok": False, "error": "No encontrado"}, 404)
            return
        if not self._token_ok():
            self._json({"ok": False, "error": "Sesión caducada: recarga la página."}, 401)
            return
        body: Dict[str, Any] = {}
        if method == "POST":
            parsed = self._body()
            if parsed is None:
                self._json({"ok": False, "error": "El cuerpo debe ser JSON."}, 400)
                return
            body = parsed
        for route_method, pattern, handler in ROUTES:
            match = re.fullmatch(pattern, parts.path)
            if match and route_method == method:
                try:
                    data, status = handler(body, query, *(unquote(g) for g in match.groups()))
                except (ls.LLMSettingsError, runs.RunError) as exc:
                    data, status = {"ok": False, "error": str(exc)}, 400
                except OSError as exc:
                    data, status = {"ok": False, "error": f"No se pudo guardar: {exc}"}, 500
                except Exception as exc:  # noqa: BLE001 - answer instead of dropping the connection
                    data, status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500
                self._json(data, status)
                return
        self._json({"ok": False, "error": "No encontrado"}, 404)


# -- API ---------------------------------------------------------------------------

Result = Tuple[Dict[str, Any], int]


def _status() -> Dict[str, Any]:
    return {"ok": True, "providers": ls.provider_status()}


def api_providers(body, query) -> Result:
    return {**_status(), "locked": _run_active()}, 200


RUN_LOCK_MESSAGE = ("Hay una limpieza en marcha: los proveedores no se pueden cambiar, quitar ni actualizar "
                    "hasta que termine. El modelo sí se puede cambiar; se aplicará a la próxima limpieza.")


def _run_active() -> bool:
    current = runs.RUNS.get()
    return bool(current and current.status in ("starting", "running"))


def _locked() -> Optional[Result]:
    """While a cleanup runs, the providers it may be using cannot change under it."""
    return ({"ok": False, "error": RUN_LOCK_MESSAGE, "locked": True}, 409) if _run_active() else None


def api_litellm(body, query) -> Result:
    if _locked():
        return _locked()
    if body.get("clear"):
        removed = ls.clear_litellm()
        return {**_status(), "removed": removed}, 200
    ls.save_litellm(str(body.get("base_url") or ""), str(body.get("api_key") or ""))
    return _status(), 200


def api_models(body, query, pid) -> Result:
    refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
    if refresh and _locked():
        return _locked()
    catalog = ls.provider_models(pid, refresh=refresh)
    status = 404 if pid not in ls.PROVIDERS else 200
    return {"ok": bool(catalog["models"]), "provider": pid, **catalog}, status


def api_copilot_login(body, query) -> Result:
    if _locked():
        return _locked()
    return {"ok": True, **ls.start_copilot_login()}, 200


def api_copilot_poll(body, query) -> Result:
    result = ls.poll_copilot_login(str(body.get("flow_id") or ""))
    return {"ok": result.get("status") not in ("error", "unknown"), **result}, 200


def api_copilot_token(body, query) -> Result:
    if _locked():
        return _locked()
    ls.save_copilot_token(str(body.get("token") or ""), method="token")
    return _status(), 200


def api_copilot_logout(body, query) -> Result:
    if _locked():
        return _locked()
    removed = ls.clear_copilot()
    return {**_status(), "removed": removed}, 200


def api_model_get(body, query) -> Result:
    return {"ok": True, "selection": ls.selection(), "effective": effective_model()}, 200


def api_model_set(body, query) -> Result:
    if body.get("clear"):
        ls.clear_selection()
    else:
        ls.save_selection(str(body.get("provider") or ""), str(body.get("model") or ""),
                          str(body.get("triage_model") or ""))
    return {"ok": True, "selection": ls.selection(), "effective": effective_model()}, 200


def api_sonar_status(body, query) -> Result:
    return {"ok": True, "sonar": sonar_setup.status()}, 200


def api_projects(body, query) -> Result:
    return {"ok": True, "target": str(runs.target_dir()), "projects": runs.list_projects()}, 200


def api_project_file(body, query, name, filename) -> Result:
    return {"ok": True, **runs.read_output(name, filename)}, 200


def api_run_start(body, query) -> Result:
    run = runs.RUNS.start(str(body.get("project") or ""), body.get("options") or {})
    return {"ok": True, "run": run.public()}, 200


def api_run_current(body, query) -> Result:
    run = runs.RUNS.get()
    return {"ok": True, "run": run.public() if run else None}, 200


def api_run_events(body, query, run_id) -> Result:
    run = runs.RUNS.get(run_id)
    if run is None:
        return {"ok": False, "error": "Ejecución desconocida."}, 404
    since = max(0, int((query.get("since") or ["0"])[0] or 0))
    return {"ok": True, "run": run.public(), "events": run.events[since:since + 2000]}, 200


def api_run_cancel(body, query) -> Result:
    run = runs.RUNS.cancel()
    return {"ok": True, "run": run.public()}, 200


ROUTES: Tuple[Tuple[str, str, Callable[..., Result]], ...] = (
    ("GET", r"/api/sonar/status", api_sonar_status),
    ("GET", r"/api/projects", api_projects),
    ("GET", r"/api/projects/([^/]+)/files/([A-Za-z_.]+)", api_project_file),
    ("POST", r"/api/runs", api_run_start),
    ("GET", r"/api/runs/current", api_run_current),
    ("GET", r"/api/runs/([A-Za-z0-9-]+)/events", api_run_events),
    ("POST", r"/api/runs/cancel", api_run_cancel),
    ("GET", r"/api/llm/providers", api_providers),
    ("POST", r"/api/llm/litellm", api_litellm),
    ("GET", r"/api/llm/providers/([a-z_]+)/models", api_models),
    ("POST", r"/api/llm/copilot/login", api_copilot_login),
    ("POST", r"/api/llm/copilot/login/poll", api_copilot_poll),
    ("POST", r"/api/llm/copilot/token", api_copilot_token),
    ("POST", r"/api/llm/copilot/logout", api_copilot_logout),
    ("GET", r"/api/config/model", api_model_get),
    ("POST", r"/api/config/model", api_model_set),
)


def serve(host: str = "127.0.0.1", port: int = 8770, token: Optional[str] = None,
          extra_hosts: Tuple[str, ...] = ()) -> DashboardServer:
    """Start the server in a background thread and return it (call .shutdown() to stop)."""
    server = DashboardServer((host, port), token=token, extra_hosts=extra_hosts)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
