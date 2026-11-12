"""Local SonarQube: is it up, and prepare it on its own so nobody has to touch it.

The SonarQube that docker compose starts next to Poda AI comes with the
default `admin`/`admin` account. The first time it is up, Poda AI:

1. changes that password for a random one (SonarQube forces the change anyway);
2. creates a user token for the analyses and the issue search.

Both are kept in `.poda-settings/providers.json` (mode 0600, never sent to
the browser). A SONAR_TOKEN given in the environment or .env wins.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from . import llm_settings as ls

SECTION = "sonar"
_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}


class SonarSetupError(RuntimeError):
    pass


def _request(method: str, url: str, auth: Optional[Tuple[str, str]] = None, token: str = "",
             params: Optional[Dict[str, str]] = None, timeout: float = 10) -> Tuple[int, Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    elif token:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"{token}:".encode()).decode())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read() or b"", exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise SonarSetupError(str(getattr(exc, "reason", exc))) from exc
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return status, {}


def sonar_url(cfg: Optional[Dict[str, str]] = None) -> str:
    cfg = cfg if cfg is not None else ls._dotenv_cfg()
    return (cfg.get("SONAR_HOST_URL") or "").strip().rstrip("/")


def _token_valid(url: str, token: str) -> bool:
    try:
        status, data = _request("GET", f"{url}/api/authentication/validate", token=token)
    except SonarSetupError:
        return False
    return status == 200 and bool(data.get("valid"))


def _login_ok(url: str, password: str) -> bool:
    status, data = _request("GET", f"{url}/api/authentication/validate", auth=("admin", password))
    return status == 200 and bool(data.get("valid"))


def _strong_password() -> str:
    # SonarQube asks for 12+ characters with upper and lower case letters, digits and symbols.
    return secrets.token_urlsafe(18) + "Aa1!"


def ensure_token(url: str, cfg: Optional[Dict[str, str]] = None) -> str:
    """A valid token for `url`, creating it on the local SonarQube when needed."""
    cfg = cfg if cfg is not None else ls._dotenv_cfg()
    stored = ls._section(SECTION)
    for candidate in ((cfg.get("SONAR_TOKEN") or "").strip(), stored.get("token", "")):
        if candidate and _token_valid(url, candidate):
            return candidate
    password = stored.get("admin_password", "")
    if not (password and _login_ok(url, password)):
        if not _login_ok(url, "admin"):
            raise SonarSetupError(
                "no se puede preparar SonarQube automáticamente: la cuenta admin ya no tiene la contraseña "
                "inicial. Crea un token en SonarQube (My Account → Security) y ponlo en SONAR_TOKEN.")
        password = _strong_password()
        status, data = _request("POST", f"{url}/api/users/change_password", auth=("admin", "admin"),
                                params={"login": "admin", "previousPassword": "admin", "password": password})
        if status not in (200, 204):
            raise SonarSetupError(f"SonarQube no aceptó el cambio de contraseña inicial (HTTP {status}).")
        ls._update_section(SECTION, {**stored, "admin_password": password})
    status, data = _request("POST", f"{url}/api/user_tokens/generate", auth=("admin", password),
                            params={"name": f"poda-{int(time.time())}"})
    token = str((data or {}).get("token") or "")
    if status != 200 or not token:
        raise SonarSetupError(f"SonarQube no creó el token de análisis (HTTP {status}).")
    ls._update_section(SECTION, {**ls._section(SECTION), "token": token, "admin_password": password})
    return token


def status(cfg: Optional[Dict[str, str]] = None, setup: bool = True, max_age: float = 4.0) -> Dict[str, Any]:
    """state: unavailable | starting | ready | error (cached a few seconds: the page polls it)."""
    now = time.time()
    if _CACHE["value"] is not None and now - _CACHE["at"] < max_age:
        return dict(_CACHE["value"])
    result = _status(cfg, setup)
    _CACHE.update(at=now, value=result)
    return dict(result)


def _status(cfg: Optional[Dict[str, str]], setup: bool) -> Dict[str, Any]:
    url = sonar_url(cfg)
    if not url:
        return {"state": "unavailable", "url": "",
                "message": "No hay ningún SonarQube configurado (SONAR_HOST_URL). Con Docker se levanta solo "
                           "con docker compose up."}
    try:
        code, data = _request("GET", f"{url}/api/system/status", timeout=5)
    except SonarSetupError:
        return {"state": "starting", "url": url,
                "message": "SonarQube todavía no responde. Si acabas de levantar los contenedores, la primera "
                           "vez tarda 1–3 minutos."}
    server_state = str((data or {}).get("status") or "")
    if code != 200 or server_state != "UP":
        return {"state": "starting", "url": url, "version": (data or {}).get("version"),
                "message": f"SonarQube está arrancando ({server_state or f'HTTP {code}'}). Espera un momento."}
    if setup:
        try:
            ensure_token(url, cfg)
        except SonarSetupError as exc:
            return {"state": "error", "url": url, "version": data.get("version"), "message": str(exc)}
    return {"state": "ready", "url": url, "version": data.get("version"), "message": "Listo para analizar."}


def forget_cache() -> None:
    _CACHE.update(at=0.0, value=None)


def stored_token() -> str:
    return ls._section(SECTION).get("token", "")
