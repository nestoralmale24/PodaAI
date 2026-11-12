"""Providers and model selection managed from the dashboard (`poda dashboard`).

What the dashboard saves wins over .env (and the CLI options win over both):

* LiteLLM gateway: URL and API key in ``<home>/.poda-settings/providers.json``
  (mode 0600). The key is never returned to the browser.
* GitHub Copilot: the GitHub token goes to ``<copilot home>/github-token``, the
  same file the local proxy and ``poda login copilot`` use, so every way of
  connecting stays compatible. "Disconnect" deletes it.
* Model selection: provider, model and triage model in the same providers.json.

``overlay(cfg)`` turns the saved values into the variables ``resolve_llm`` reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import (DEFAULT_LITELLM_BASE_URL, EXAMPLE_MARKERS, PROJECT_ROOT, copilot_env, copilot_home,
                     normalize_base_url, setting)
from .providers import copilot_pricing

PROVIDERS = ("litellm", "copilot")
PROVIDER_LABELS = {"litellm": "LiteLLM gateway", "copilot": "GitHub Copilot"}
SETTINGS_FILE = "providers.json"
MODELS_TTL_SECONDS = {"litellm": 600, "copilot": 1800}
COPILOT_CLIENT_ID = os.environ.get("COPILOT_CLIENT_ID", "Iv1.b507a08c87ecfe98")
USER_AGENT = "poda"

_LOCK = threading.RLock()


class LLMSettingsError(RuntimeError):
    pass


def github_web() -> str:
    return os.environ.get("COPILOT_GITHUB_WEB", "https://github.com").rstrip("/")


def github_api() -> str:
    return os.environ.get("COPILOT_GITHUB_API", "https://api.github.com").rstrip("/")


# ── storage ──────────────────────────────────────────────────────────────────

def settings_dir() -> Path:
    raw = os.environ.get("PODA_SETTINGS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    path = PROJECT_ROOT / ".poda-settings"
    legacy = PROJECT_ROOT / ".deadcode-settings"  # saved before the rename to Poda AI
    if not path.exists() and legacy.is_dir():
        try:
            legacy.rename(path)
        except OSError:
            return legacy
    return path


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_settings() -> Dict[str, Any]:
    try:
        data = json.loads((settings_dir() / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(name: str) -> Dict[str, str]:
    value = load_settings().get(name)
    return {str(k): str(v) for k, v in value.items() if v not in (None, "")} if isinstance(value, dict) else {}


def _update_section(name: str, values: Optional[Dict[str, str]]) -> None:
    with _LOCK:
        data = load_settings()
        if values:
            data[name] = values
        else:
            data.pop(name, None)
        _write_private(settings_dir() / SETTINGS_FILE, json.dumps(data, indent=1, ensure_ascii=False))


def _dotenv_cfg() -> Dict[str, str]:
    from .config import load_env
    return load_env(dashboard=False)


# ── LiteLLM gateway ──────────────────────────────────────────────────────────

def litellm_removed() -> bool:
    """LiteLLM was removed from the dashboard: its values in .env are ignored as well."""
    return _section("litellm").get("removed") == "1"


def litellm_config(cfg: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    stored = _section("litellm")
    env = {} if stored.get("removed") == "1" else (cfg if cfg is not None else _dotenv_cfg())
    env_url = setting(env, "LITELLM_BASE_URL")
    url = stored.get("base_url") or env_url or DEFAULT_LITELLM_BASE_URL
    key = stored.get("api_key") or setting(env, "LITELLM_API_KEY")
    return {
        "base_url": normalize_base_url(url),
        "api_key": key,
        "url_source": "dashboard" if stored.get("base_url") else ("env" if env_url else "default"),
        "key_source": "dashboard" if stored.get("api_key") else ("env" if key else ""),
    }


def litellm_errors(base_url: str, api_key: str) -> List[str]:
    errors = []
    if not base_url:
        errors.append("Falta la URL del gateway.")
    elif not re.match(r"^https?://[^\s/]+", base_url):
        errors.append("La URL del gateway debe empezar por http:// o https://.")
    elif "#" in base_url:
        errors.append("La URL contiene '#': parece la de un portal web, no la de la API.")
    elif any(m in base_url for m in EXAMPLE_MARKERS):
        errors.append("La URL del gateway todavía es el valor de ejemplo.")
    if not api_key:
        errors.append("Falta la API key (obligatoria).")
    elif any(m in api_key for m in EXAMPLE_MARKERS):
        errors.append("La API key todavía es el valor de ejemplo.")
    return errors


def save_litellm(base_url: str, api_key: Optional[str]) -> None:
    """Store URL and key. An empty key keeps the saved one (or the one in .env)."""
    stored = _section("litellm")
    url = normalize_base_url(str(base_url or ""))  # empty: the default gateway (or LITELLM_BASE_URL)
    new_key = str(api_key or "").strip()
    kept = stored.get("api_key", "")
    env_key = "" if stored.get("removed") == "1" else setting(_dotenv_cfg(), "LITELLM_API_KEY")
    effective = new_key or kept or env_key
    errors = litellm_errors(url or litellm_config()["base_url"], effective)
    if errors:
        raise LLMSettingsError(" ".join(errors))
    values = {"base_url": url} if url else {}
    if new_key or kept:
        values["api_key"] = new_key or kept
    _update_section("litellm", values)
    _drop_models_cache("litellm")


def clear_litellm() -> bool:
    """Remove LiteLLM for real: saved URL/key, the .env values (ignored from now on), the
    model list and a model selection that used it. Saving a new API key brings it back."""
    existed = bool(litellm_config()["api_key"])
    _update_section("litellm", {"removed": "1"})
    _drop_models_cache("litellm")
    _drop_selection_of("litellm")
    return existed


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None,
          timeout: float = 20) -> Tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LLMSettingsError(f"No se pudo conectar con {urllib.parse.urlparse(url).netloc}: {reason}") from exc


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _fetch_litellm_models(base_url: str, api_key: str) -> List[Dict[str, str]]:
    url = f"{base_url}/v1/models"
    status, raw = _http("GET", url, {"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    if status in (401, 403):
        raise LLMSettingsError(f"El gateway LiteLLM rechaza la API key (HTTP {status}).")
    if status != 200:
        raise LLMSettingsError(f"Respuesta inesperada de {url} (HTTP {status}).")
    data = _json(raw)
    if not isinstance(data, dict):
        raise LLMSettingsError(f"{url} no devuelve JSON: comprueba que la URL es la de la API, no la de un portal.")
    seen, models = set(), []
    for item in data.get("data") or []:
        model_id = str((item or {}).get("id") or "").strip() if isinstance(item, dict) else ""
        if model_id and "*" not in model_id and model_id not in seen:  # "x/*" are routes, not models
            seen.add(model_id)
            models.append({"id": model_id, "label": model_id})
    if not models:
        raise LLMSettingsError("La API key no tiene ningún modelo disponible en el gateway.")
    return sorted(models, key=lambda m: m["id"].lower())


# ── GitHub Copilot ───────────────────────────────────────────────────────────

def _token_file(cfg: Optional[Dict[str, str]] = None) -> Path:
    return copilot_home(cfg if cfg is not None else _dotenv_cfg()) / "github-token"


def copilot_config(cfg: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    try:
        token = _token_file(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    stored = _section("copilot")
    return {"token": token, "account": stored.get("account", "") if token else "",
            "method": stored.get("method", "cli") if token else ""}


def copilot_token_error(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return "El token está vacío."
    if re.search(r"\s", token):
        return "El token contiene espacios: pégalo de nuevo sin comillas."
    if token.startswith(("github_pat_", "ghp_")):
        return ("Poda AI usa la API de Copilot de los editores, que no acepta tokens personales (github_pat_ o ghp_). "
                "Usa «Conectar con GitHub».")
    if not re.match(r"^(gho_|ghu_)[A-Za-z0-9_]{10,}$", token):
        return "Token no reconocido: debe ser un token OAuth de GitHub (gho_…). Usa «Conectar con GitHub»."
    return ""


def github_account(token: str) -> str:
    try:
        status, raw = _http("GET", f"{github_api()}/user", {
            "Authorization": f"token {token}", "Accept": "application/json", "User-Agent": USER_AGENT}, timeout=15)
    except LLMSettingsError:
        return ""
    data = _json(raw)
    return str(data.get("login") or "") if status == 200 and isinstance(data, dict) else ""


def save_copilot_token(token: str, method: str = "token") -> str:
    token = str(token or "").strip()
    error = copilot_token_error(token)
    if error:
        raise LLMSettingsError(error)
    account = github_account(token)
    _write_private(_token_file(), token + "\n")
    _update_section("copilot", {"account": account, "method": method})
    _drop_models_cache("copilot")
    return account


def stop_copilot_proxy() -> bool:
    """Ask a running local Copilot proxy to exit (it keeps a Copilot token in memory)."""
    cfg = _dotenv_cfg()
    key_file = copilot_home(cfg) / "proxy-key"
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    port = int(cfg.get("COPILOT_PROXY_PORT") or 4142)
    try:
        status, _ = _http("POST", f"http://127.0.0.1:{port}/shutdown",
                          {"x-api-key": key, "Content-Type": "application/json"}, body={}, timeout=5)
    except LLMSettingsError:
        return False
    return status == 200


def clear_copilot() -> bool:
    """Disconnect for real: stop the proxy, delete the GitHub session and the proxy key,
    the model list and a model selection that used Copilot."""
    stop_copilot_proxy()
    home = copilot_home(_dotenv_cfg())
    existed = (home / "github-token").is_file()
    for name in ("github-token", "proxy-key"):
        try:
            (home / name).unlink()
        except OSError:
            pass
    _update_section("copilot", None)
    _drop_models_cache("copilot")
    _drop_selection_of("copilot")
    return existed


def _fetch_copilot_models() -> List[Dict[str, str]]:
    cfg = _dotenv_cfg()
    try:
        out = subprocess.run([sys.executable, "-m", "poda.providers.copilot_proxy", "models", "--describe"],
                             env=copilot_env(cfg), cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMSettingsError(f"No se pudo consultar GitHub Copilot: {exc}") from exc
    if out.returncode != 0:
        raise LLMSettingsError((out.stderr or "").strip().lstrip("✗ ") or "GitHub Copilot no devolvió modelos.")
    models = []
    for line in out.stdout.splitlines():
        model_id, _, description = line.strip().partition("\t")
        if model_id:
            models.append({"id": model_id, "label": f"{model_id} · {description}" if description else model_id})
    if not models:
        raise LLMSettingsError("GitHub Copilot no devolvió ningún modelo para esta cuenta.")
    return models


# Device flow: the device code never leaves the server; the browser only gets a flow id.
_FLOWS: Dict[str, Dict[str, Any]] = {}
_FLOWS_LOCK = threading.Lock()


def start_copilot_login() -> Dict[str, Any]:
    status, raw = _http("POST", f"{github_web()}/login/device/code", {
        "Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT,
    }, body={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"})
    data = _json(raw) or {}
    if status != 200 or not isinstance(data, dict) or not data.get("device_code"):
        raise LLMSettingsError(f"GitHub no ha iniciado el inicio de sesión (HTTP {status}).")
    flow_id = secrets.token_urlsafe(16)
    interval = int(data.get("interval") or 5)
    expires_in = int(data.get("expires_in") or 900)
    with _FLOWS_LOCK:
        now = time.time()
        for key in [k for k, v in _FLOWS.items() if v["expires_at"] < now]:
            _FLOWS.pop(key, None)
        _FLOWS[flow_id] = {"device_code": str(data["device_code"]), "interval": interval,
                           "expires_at": now + expires_in, "next_poll": now + interval,
                           "status": "pending", "message": ""}
    return {"flow_id": flow_id, "user_code": str(data.get("user_code") or ""),
            "verification_uri": str(data.get("verification_uri") or f"{github_web()}/login/device"),
            "interval": interval, "expires_in": expires_in}


def poll_copilot_login(flow_id: str) -> Dict[str, Any]:
    """Ask GitHub at most once per interval; saves the token once it is authorised."""
    with _FLOWS_LOCK:
        flow = _FLOWS.get(flow_id)
    if flow is None:
        return {"status": "unknown", "message": "Inicio de sesión desconocido o terminado: vuelve a empezar."}
    if flow["status"] != "pending":
        return {"status": flow["status"], "message": flow["message"], "interval": flow["interval"]}
    now = time.time()
    if now > flow["expires_at"]:
        flow.update(status="expired", message="El código ha caducado sin autorizarse. Vuelve a empezar.")
        return {"status": "expired", "message": flow["message"], "interval": flow["interval"]}
    if now < flow["next_poll"]:
        return {"status": "pending", "message": "", "interval": flow["interval"]}
    try:
        status, raw = _http("POST", f"{github_web()}/login/oauth/access_token", {
            "Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT,
        }, body={"client_id": COPILOT_CLIENT_ID, "device_code": flow["device_code"],
                 "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        result = _json(raw) or {}
    except LLMSettingsError as exc:
        status, result = 0, {"error": "authorization_pending", "error_description": str(exc)}
    flow["next_poll"] = time.time() + flow["interval"]
    token = str(result.get("access_token") or "") if isinstance(result, dict) else ""
    error = str(result.get("error") or "") if isinstance(result, dict) else ""
    if token:
        try:
            account = save_copilot_token(token, method="device")
        except (LLMSettingsError, OSError) as exc:
            flow.update(status="error", message=f"No se pudo guardar la sesión: {exc}")
        else:
            flow.update(status="authorized", message=f"Conectado como {account}" if account else "Conectado")
    elif error in ("authorization_pending", ""):
        if not error and status not in (0, 200):
            flow.update(status="error", message=f"GitHub respondió HTTP {status}.")
    elif error == "slow_down":
        flow["interval"] = int(result.get("interval") or flow["interval"] + 5)
    elif error == "expired_token":
        flow.update(status="expired", message="El código ha caducado sin autorizarse. Vuelve a empezar.")
    elif error == "access_denied":
        flow.update(status="denied", message="Se ha cancelado la autorización en GitHub.")
    else:
        flow.update(status="error", message=str(result.get("error_description") or error))
    if flow["status"] != "pending":
        flow.pop("device_code", None)
    return {"status": flow["status"], "message": flow["message"], "interval": flow["interval"]}


# ── model catalogues (cached per credentials) ────────────────────────────────

def _identity(pid: str) -> str:
    if pid == "litellm":
        c = litellm_config()
        seed = f"{c['base_url']}\0{c['api_key']}"
    else:
        seed = copilot_config()["token"]
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _cache_path(pid: str) -> Path:
    return settings_dir() / f"{pid}-models.json"


def _drop_models_cache(pid: str) -> None:
    try:
        _cache_path(pid).unlink()
    except OSError:
        pass


def cached_models(pid: str) -> Optional[List[Dict[str, str]]]:
    try:
        data = json.loads(_cache_path(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("identity") != _identity(pid):
        return None
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("id")] or None


def _usd(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _with_prices(pid: str, models: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Copilot models show GitHub's published rate, so the cost of each one is visible before choosing."""
    if pid != "copilot":
        return models
    out = []
    for m in models:
        price = copilot_pricing.lookup(m.get("id", ""))
        label = m.get("label") or m.get("id", "")
        if price:
            label += f" · {_usd(price.base.input)} $ / {_usd(price.base.output)} $ por M tokens"
        out.append({**m, "label": label})
    return out


def provider_models(pid: str, refresh: bool = False) -> Dict[str, Any]:
    if pid not in PROVIDERS:
        return {"models": [], "source": "none", "error": f"Proveedor desconocido: {pid}"}
    problems = provider_errors(pid)
    if problems:
        return {"models": [], "source": "none", "error": problems[0]}
    if not refresh:
        try:
            data = json.loads(_cache_path(pid).read_text(encoding="utf-8"))
            fresh = time.time() - float(data.get("fetched_at") or 0) < MODELS_TTL_SECONDS[pid]
            if data.get("identity") == _identity(pid) and fresh and data.get("models"):
                return {"models": _with_prices(pid, data["models"]), "source": "cache", "error": ""}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    try:
        if pid == "litellm":
            c = litellm_config()
            models = _fetch_litellm_models(c["base_url"], c["api_key"])
        else:
            models = _fetch_copilot_models()
    except LLMSettingsError as exc:
        stale = cached_models(pid)
        return {"models": _with_prices(pid, stale or []), "source": "cache" if stale else "none", "error": str(exc)}
    try:
        _write_private(_cache_path(pid), json.dumps({"fetched_at": time.time(), "identity": _identity(pid),
                                                     "models": models}, ensure_ascii=False))
    except OSError:
        pass
    return {"models": _with_prices(pid, models), "source": "live", "error": ""}


# ── status for the dashboard (never any secret) ──────────────────────────────

def provider_errors(pid: str) -> List[str]:
    if pid == "litellm":
        c = litellm_config()
        return litellm_errors(c["base_url"], c["api_key"])
    if pid == "copilot":
        return [] if copilot_config()["token"] else ["No hay sesión de GitHub Copilot: conéctala con GitHub."]
    return [f"Proveedor desconocido: {pid}"]


def provider_status() -> List[Dict[str, Any]]:
    lite = litellm_config()
    lite_errors = litellm_errors(lite["base_url"], lite["api_key"])
    if litellm_removed():
        lite_errors = ["Quitado: pega una API key para volver a usarlo."]
    cop = copilot_config()
    if cop["token"]:
        cop_message = f"Conectado como {cop['account']}" if cop["account"] else "Conectado"
    else:
        cop_message = "Sin conectar: conecta con GitHub o pega un token."
    return [
        {"provider": "litellm", "label": PROVIDER_LABELS["litellm"],
         "configured": bool(lite["base_url"] and lite["api_key"]), "ready": not lite_errors,
         "source": lite["key_source"] or lite["url_source"], "url_source": lite["url_source"],
         "key_source": lite["key_source"], "base_url": lite["base_url"], "api_key_set": bool(lite["api_key"]),
         "default_url": DEFAULT_LITELLM_BASE_URL, "removed": litellm_removed(),
         "message": " ".join(lite_errors) if lite_errors else f"Gateway {lite['base_url']}"},
        {"provider": "copilot", "label": PROVIDER_LABELS["copilot"], "configured": bool(cop["token"]),
         "ready": bool(cop["token"]), "source": "dashboard" if cop["method"] in ("device", "token") else
         ("cli" if cop["token"] else ""), "account": cop["account"], "message": cop_message},
    ]


# ── model selection ──────────────────────────────────────────────────────────

def selection() -> Dict[str, str]:
    stored = _section("selection")
    return {"provider": stored.get("provider", ""), "model": stored.get("model", ""),
            "triage_model": stored.get("triage_model", "")}


def save_selection(provider: str, model: str, triage_model: str = "") -> Dict[str, str]:
    pid = str(provider or "").strip().lower()
    if pid not in PROVIDERS:
        raise LLMSettingsError(f"Proveedor desconocido: {provider!r}. Usa litellm o copilot.")
    model, triage_model = str(model or "").strip(), str(triage_model or "").strip()
    if not model:
        raise LLMSettingsError("Elige un modelo.")
    known = cached_models(pid)
    if known is not None:
        ids = {m["id"] for m in known}
        for name in (model, triage_model):
            if name and name not in ids:
                raise LLMSettingsError(f"{PROVIDER_LABELS[pid]} no ofrece el modelo `{name}`.")
    values = {"provider": pid, "model": model}
    if triage_model and triage_model != model:
        values["triage_model"] = triage_model
    _update_section("selection", values)
    return selection()


def _drop_selection_of(pid: str) -> None:
    if _section("selection").get("provider") == pid:
        _update_section("selection", None)


def clear_selection() -> bool:
    existed = bool(_section("selection"))
    _update_section("selection", None)
    return existed


def overlay(cfg: Dict[str, str]) -> Dict[str, str]:
    """Apply the dashboard values to a configuration dict (they win over .env)."""
    data = load_settings()
    lite = data.get("litellm") if isinstance(data.get("litellm"), dict) else {}
    if lite.get("removed") == "1":
        cfg.pop("LITELLM_API_KEY", None)
        cfg.pop("LITELLM_BASE_URL", None)
    if lite.get("base_url"):
        cfg["LITELLM_BASE_URL"] = str(lite["base_url"])
    if lite.get("api_key"):
        cfg["LITELLM_API_KEY"] = str(lite["api_key"])
    sel = data.get("selection") if isinstance(data.get("selection"), dict) else {}
    pid, model = str(sel.get("provider") or ""), str(sel.get("model") or "")
    if pid in PROVIDERS and model:
        cfg["LLM_PROVIDER"] = pid
        cfg["LITELLM_MODEL" if pid == "litellm" else "COPILOT_MODEL"] = model
        cfg["PODA_TRIAGE_MODEL"] = str(sel.get("triage_model") or model)
        cfg["PODA_SELECTION_SOURCE"] = "dashboard"
    sonar = data.get("sonar") if isinstance(data.get("sonar"), dict) else {}
    if sonar.get("token") and not (cfg.get("SONAR_TOKEN") or "").strip():
        cfg["SONAR_TOKEN"] = str(sonar["token"])  # created by poda.sonar_setup on the local SonarQube
    return cfg
