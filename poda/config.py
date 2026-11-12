"""Configuration and LLM provider resolution.

Precedence: CLI options > dashboard (`poda dashboard`) > .env > process environment > defaults. The .env file
wins over the environment so that editing it takes effect without recreating
the container.

Every provider ends up as a LiteLLM model string plus optional api_base/api_key:

    litellm         corporate LiteLLM gateway   → litellm_proxy/<alias>  (LITELLM_BASE_URL, LITELLM_API_KEY)
    copilot         local Copilot proxy         → anthropic/<model>      (poda/providers/copilot_proxy.py)
    github_copilot  LiteLLM native provider     → github_copilot/<model> (device flow handled by LiteLLM)
    bedrock         AWS Bedrock                 → bedrock/<model id or ARN> (AWS_REGION, AWS credentials)
    anthropic       Anthropic API               → anthropic/<model>      (ANTHROPIC_API_KEY)
    <other>         any LiteLLM provider        → <provider>/<model>     (openai, azure, gemini, vertex_ai...)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

def _home() -> Path:
    """Where .env and .copilot/ live: $PODA_HOME, the source checkout, or ~/.poda."""
    explicit = os.environ.get("PODA_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    source = Path(__file__).resolve().parents[1]
    if (source / "pyproject.toml").is_file():
        return source
    return Path.home() / ".poda"


PROJECT_ROOT = _home()


def output_root() -> Path:
    """Where clean copies and reports go, apart from the apps in target/: $PODA_OUTPUT_DIR or <home>/output."""
    raw = os.environ.get("PODA_OUTPUT_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else (PROJECT_ROOT / "output").resolve()


def default_output_dir(project: Path) -> Path:
    """output/<app>/: the clean copy (<app>/), the reports and the AI cache (.poda/)."""
    return output_root() / project.name


EXAMPLE_MARKERS = ("REEMPLAZAR", "example.com", "example.org")
# Gateway LiteLLM que se usa si no se indica otro (dashboard, LITELLM_BASE_URL o --api-base).
DEFAULT_LITELLM_BASE_URL = os.environ.get("PODA_DEFAULT_LITELLM_URL") or "https://litellm.apps.hiberus.tech"
SETUP_HINT = "Abre  poda dashboard  y configura LiteLLM (basta con la API key) o GitHub Copilot."


class ConfigError(RuntimeError):
    pass


def read_dotenv(path: Path) -> Dict[str, str]:
    """Parse a .env file without executing it (comments, quotes, `export`, CRLF)."""
    values: Dict[str, str] = {}
    if not path or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if not key.replace("_", "").isalnum():
            continue
        if value[:1] in ("'", '"'):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def load_env(env_file: Optional[Path] = None, dashboard: bool = True) -> Dict[str, str]:
    """Process environment, updated with the .env file and then with the dashboard settings.

    The .env file is --env-file, $PODA_ENV_FILE or <home>/.env. An explicit
    path is relative to the current directory; the default .env is the one next
    to Poda AI, never one that happens to be in the analysed project. What was
    saved from `poda dashboard` (gateway, model selection) wins over .env.
    """
    cfg: Dict[str, str] = dict(os.environ)
    named = env_file or os.environ.get("PODA_ENV_FILE") or os.environ.get("LLM_ENV_FILE")
    path = Path(named).expanduser() if named else PROJECT_ROOT / ".env"
    cfg.update(read_dotenv(path))
    for name, value in list(cfg.items()):  # formerly "deadcode": DEADCODE_* still works as PODA_*
        if name.startswith("DEADCODE_"):
            cfg.setdefault("PODA_" + name[len("DEADCODE_"):], value)
    if dashboard:
        from .llm_settings import overlay
        overlay(cfg)
    return cfg


@dataclass
class LLMConfig:
    provider: str
    model: str                         # LiteLLM model string
    triage_model: str                  # cheaper model for snippet validation
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    display: str = ""

    def call_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = dict(self.extra)
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        return kwargs


def normalize_base_url(url: str) -> str:
    previous = None
    url = url.strip()
    while url != previous:
        previous = url
        url = url.rstrip("/")
        for suffix in ("/v1/messages", "/v1/chat/completions", "/chat/completions", "/v1"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
    return url


def is_placeholder(value: Optional[str]) -> bool:
    text = (value or "").strip()
    return any(marker in text for marker in EXAMPLE_MARKERS) or text.endswith("-example")


def setting(cfg: Dict[str, str], name: str) -> str:
    """Value of a setting, ignoring empty values and the placeholders of .env.sample."""
    value = (cfg.get(name) or "").strip()
    return "" if is_placeholder(value) else value


def _check(value: Optional[str], what: str, hint: str) -> str:
    if not value or is_placeholder(value):
        raise ConfigError(f"Falta {what}. {hint}")
    return value


def copilot_logged_in(cfg: Dict[str, str]) -> bool:
    return (copilot_home(cfg) / "github-token").is_file()


def _qualify(provider: str, model: str) -> str:
    prefix = {"litellm": "litellm_proxy", "copilot": "anthropic"}.get(provider, provider)
    return model if model.startswith(prefix + "/") else f"{prefix}/{model}"


def detect_provider(cfg: Dict[str, str], cli_provider: Optional[str], cli_model: Optional[str]) -> str:
    provider = (cli_provider or "").strip().lower()
    if provider:
        return provider
    if cli_model and "/" in cli_model and not (cfg.get("LLM_PROVIDER") or "").strip():
        return cli_model.split("/", 1)[0]
    provider = setting(cfg, "LLM_PROVIDER").lower()
    if provider:
        return provider
    if cfg.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        return "bedrock"
    if setting(cfg, "LITELLM_API_KEY"):
        return "litellm"
    if copilot_logged_in(cfg):
        return "copilot"
    raise ConfigError("No hay ningún proveedor de IA configurado. " + SETUP_HINT)


def resolve_llm(cfg: Dict[str, str], cli_provider: Optional[str] = None, cli_model: Optional[str] = None,
                cli_triage: Optional[str] = None, cli_api_base: Optional[str] = None,
                start_proxy: bool = True) -> LLMConfig:
    provider = detect_provider(cfg, cli_provider, cli_model)
    explicit_model = cli_model
    if provider == "litellm":
        base = normalize_base_url(cli_api_base or setting(cfg, "LITELLM_BASE_URL") or DEFAULT_LITELLM_BASE_URL)
        if "#" in base:
            raise ConfigError("La URL del gateway contiene '#': parece la URL de un portal web, no de la API.")
        key = _check(setting(cfg, "LITELLM_API_KEY"), "la API key de LiteLLM (LITELLM_API_KEY), que es obligatoria",
                     "Pégala en  poda dashboard  (LiteLLM gateway) o ponla en .env.")
        model = _check(explicit_model or setting(cfg, "LITELLM_MODEL"), "el modelo de LiteLLM",
                       "Elígelo en  poda dashboard  (o con --model).")
        triage = cli_triage or setting(cfg, "PODA_TRIAGE_MODEL") or setting(cfg, "LITELLM_SMALL_MODEL") or model
        return LLMConfig("litellm", _qualify("litellm", model), _qualify("litellm", triage), base, key,
                         display=f"LiteLLM gateway · {model} · {base}")
    if provider == "copilot":
        model = _check(explicit_model or setting(cfg, "COPILOT_MODEL"), "el modelo de GitHub Copilot",
                       "Elígelo en  poda dashboard  (o con --model).")
        triage = cli_triage or setting(cfg, "PODA_TRIAGE_MODEL") or setting(cfg, "COPILOT_SMALL_MODEL") or model
        base, key = ensure_copilot_proxy(cfg) if start_proxy else ("http://127.0.0.1", "")
        return LLMConfig("copilot", _qualify("copilot", model), _qualify("copilot", triage), base, key,
                         display=f"GitHub Copilot (proxy local) · {model}")
    if provider == "bedrock":
        model = _check(explicit_model or cfg.get("ANTHROPIC_MODEL"), "ANTHROPIC_MODEL",
                       "Pon el id o ARN del modelo de Bedrock en .env o en --model.")
        region = _check(cfg.get("AWS_REGION"), "AWS_REGION", "Indica la región de Bedrock en .env.")
        triage = cli_triage or cfg.get("PODA_TRIAGE_MODEL") or model
        extra = {"aws_region_name": region}
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            if cfg.get(name):
                extra[name.lower()] = cfg[name]
        return LLMConfig("bedrock", _qualify("bedrock", model), _qualify("bedrock", triage), extra=extra,
                         display=f"AWS Bedrock · {model} · {region}")
    fallback = {"anthropic": "ANTHROPIC_MODEL", "github_copilot": "COPILOT_MODEL"}.get(provider)
    model = explicit_model or cfg.get("PODA_MODEL") or (cfg.get(fallback) if fallback else None)
    if not model:
        raise ConfigError(f"Falta el modelo para el proveedor '{provider}': usa --model.")
    triage = cli_triage or cfg.get("PODA_TRIAGE_MODEL") or model
    api_base = cli_api_base or cfg.get("PODA_API_BASE") or None
    extra: Dict[str, Any] = {}
    key = None
    if provider == "anthropic" and cfg.get("ANTHROPIC_API_KEY"):
        key = cfg["ANTHROPIC_API_KEY"]
    return LLMConfig(provider, _qualify(provider, model), _qualify(provider, triage), api_base, key, extra,
                     display=f"{provider} · {model}" + (f" · {api_base}" if api_base else ""))


# ---------------------------------------------------------------------------
# Local GitHub Copilot proxy (poda/providers/copilot_proxy.py)
# ---------------------------------------------------------------------------

def copilot_home(cfg: Dict[str, str]) -> Path:
    home = Path(cfg.get("COPILOT_HOME") or ".copilot")
    return home if home.is_absolute() else PROJECT_ROOT / home


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def copilot_env(cfg: Dict[str, str]) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({k: v for k, v in cfg.items() if k.startswith("COPILOT_")})
    env["COPILOT_HOME"] = str(copilot_home(cfg))
    return env


def ensure_copilot_proxy(cfg: Dict[str, str]) -> Tuple[str, str]:
    home = copilot_home(cfg)
    if not (home / "github-token").is_file():
        raise ConfigError("No hay sesión de GitHub Copilot. Conéctala en  poda dashboard  "
                          "(o con  poda login copilot).")
    port = int(cfg.get("COPILOT_PROXY_PORT") or 4142)
    url = f"http://127.0.0.1:{port}"
    if not _healthy(url):
        home.mkdir(parents=True, exist_ok=True)
        log = open(home / "proxy.log", "ab")
        kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                                  "cwd": str(PROJECT_ROOT), "env": copilot_env(cfg)}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "poda.providers.copilot_proxy", "serve", "--port", str(port)],
                         **kwargs)
        deadline = time.time() + 15
        while time.time() < deadline and not _healthy(url):
            time.sleep(0.3)
        if not _healthy(url):
            raise ConfigError(f"El proxy de Copilot no ha arrancado; revisa {home / 'proxy.log'}")
    key_file = home / "proxy-key"
    key = key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else ""
    if not key:
        raise ConfigError(f"Falta la clave del proxy de Copilot ({key_file}).")
    return url, key
