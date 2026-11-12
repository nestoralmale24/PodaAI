"""Command line: poda run | check | models | login."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import urllib.request
from enum import Enum
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from . import __version__
from .config import ConfigError, LLMConfig, copilot_env, load_env, resolve_llm
from .events import EventBus
from .pipeline import Pipeline, PipelineError, RunOptions
from .utils.ui import PlainUI, RichUI

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="Poda AI: elimina el código muerto de una aplicación legacy antes de documentarla o migrarla. "
                       "Genera una copia limpia, poda_report.md y execution_metrics.json/.md; "
                       "el original no se modifica.")


class Confidence(str, Enum):
    high = "high"
    medium = "medium"


class AgentMode(str, Enum):
    auto = "auto"
    always = "always"
    never = "never"


PROVIDER_HELP = ("litellm (gateway corporativo) · copilot (proxy local) · github_copilot (LiteLLM nativo) · "
                 "bedrock · anthropic · openai · azure · gemini · cualquier proveedor de LiteLLM")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"Poda AI {__version__}")
        raise typer.Exit()


@app.callback()
def _root(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                       help="Muestra la versión.")) -> None:
    """Poda AI"""


def list_models(cfg, llm_provider: str) -> List[str]:
    provider = llm_provider.lower()
    if provider == "litellm":
        from .config import normalize_base_url
        base = normalize_base_url(cfg.get("LITELLM_BASE_URL") or "")
        request = urllib.request.Request(f"{base}/v1/models",
                                         headers={"Authorization": f"Bearer {cfg.get('LITELLM_API_KEY', '')}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted({m.get("id") for m in data.get("data", []) if m.get("id") and "*" not in m.get("id")})
    if provider == "copilot":
        out = subprocess.run([sys.executable, "-m", "poda.providers.copilot_proxy", "models", "--ids"],
                             env=copilot_env(cfg), capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise ConfigError(out.stderr.strip() or "no se pudo obtener la lista de modelos de Copilot")
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    import litellm
    return sorted(litellm.models_by_provider.get(provider, []))


def _confirm(console: Console, cfg, llm: LLMConfig, triage: Optional[str], api_base: Optional[str]) -> Optional[LLMConfig]:
    while True:
        console.print(Panel.fit(f"[bold]{llm.display}[/bold]\nmodelo: {llm.model}"
                                + (f"\ntriaje: {llm.triage_model}" if llm.triage_model != llm.model else ""),
                                title="Proveedor LLM", border_style="cyan"))
        choice = Prompt.ask("\\[Enter] empezar · \\[m] elegir modelo · \\[q] salir", default="", show_default=False,
                            console=console).strip().lower()
        if choice == "":
            return llm
        if choice == "q":
            return None
        if choice == "m":
            try:
                models = list_models(cfg, llm.provider)
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]No se pudo listar los modelos: {exc}[/red]")
                continue
            for number, name in enumerate(models[:60], start=1):
                console.print(f"  {number:3d}. {name}")
            picked = Prompt.ask("Número del modelo", default="", show_default=False, console=console).strip()
            if picked.isdigit() and 1 <= int(picked) <= min(60, len(models)):
                try:
                    llm = resolve_llm(cfg, llm.provider, models[int(picked) - 1], triage, api_base)
                except ConfigError as exc:
                    console.print(f"[red]{exc}[/red]")


@app.command()
def run(
    project: Path = typer.Argument(..., help="Directorio de la aplicación legacy (no se modifica)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Carpeta de salida (por defecto output/<proyecto>)."),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=PROVIDER_HELP),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Modelo (alias del gateway, id de Copilot o cadena de LiteLLM como openai/gpt-4o)."),
    triage_model: Optional[str] = typer.Option(None, "--triage-model", help="Modelo más barato para el modo snippet (por defecto, el mismo)."),
    api_base: Optional[str] = typer.Option(None, "--api-base", help="URL base del proveedor (gateway, Azure, Ollama...)."),
    env_file: Optional[Path] = typer.Option(None, "--env-file", exists=True, dir_okay=False, help="Fichero .env alternativo."),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar nada: ejecución autónoma."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Solo análisis estático, sin IA."),
    no_discover: bool = typer.Option(False, "--no-discover", help="Omitir la fase de descubrimiento con IA."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Analizar e informar sin escribir la copia limpia."),
    resume: bool = typer.Option(False, "--resume", help="Reutilizar las respuestas de la IA de una ejecución anterior."),
    force: bool = typer.Option(False, "--force", help="Rehacer una salida anterior desde cero."),
    min_confidence: Confidence = typer.Option(Confidence.high, "--min-confidence", help="Confianza mínima de la IA para eliminar."),
    agent_mode: AgentMode = typer.Option(AgentMode.auto, "--agent-mode", help="auto: snippet y agente si hace falta · always: agente siempre · never: solo snippet."),
    batch_size: int = typer.Option(15, "--batch-size", help="Hallazgos por llamada en modo snippet."),
    agent_batch_size: int = typer.Option(5, "--agent-batch-size", help="Hallazgos por ejecución del agente."),
    jobs: Optional[int] = typer.Option(None, "--jobs", "-j", help="Llamadas al modelo en paralelo (por defecto PODA_JOBS o 2)."),
    max_candidates: int = typer.Option(0, "--max-candidates", help="Máximo de hallazgos que se envían a la IA (0 = todos)."),
    max_turns: Optional[int] = typer.Option(None, "--max-turns", help="Máximo de turnos por ejecución del agente."),
    timeout: int = typer.Option(600, "--timeout", help="Segundos máximos por llamada al modelo."),
    max_output_tokens: int = typer.Option(8000, "--max-output-tokens", help="Tokens máximos por respuesta."),
    price_in: Optional[float] = typer.Option(None, "--price-in", help="USD por millón de tokens de entrada, si el proveedor no da coste (o PODA_PRICE_INPUT_PER_MTOK)."),
    price_out: Optional[float] = typer.Option(None, "--price-out", help="USD por millón de tokens de salida (o PODA_PRICE_OUTPUT_PER_MTOK)."),
    exclude: List[str] = typer.Option([], "--exclude", help="Directorio o patrón que no se analiza (repetible)."),
    sonar: bool = typer.Option(False, "--sonar", help="Añadir el análisis de SonarQube (SONAR_HOST_URL y SONAR_TOKEN)."),
    sonar_project_key: Optional[str] = typer.Option(None, "--sonar-project-key", help="Usar un análisis existente de SonarQube."),
    plain: bool = typer.Option(False, "--plain", help="Salida línea a línea (sin panel en vivo)."),
    no_color: bool = typer.Option(False, "--no-color", help="Sin colores."),
) -> None:
    """Analiza el proyecto, valida con IA lo dudoso y genera la copia limpia, el informe y las métricas."""
    console = Console(highlight=False, no_color=no_color)
    if not project.is_dir():
        console.print(f"[red]✗ No existe el directorio del proyecto: {project.resolve()}[/red]")
        raise typer.Exit(2)
    cfg = load_env(env_file)
    try:
        jobs = jobs or int(cfg.get("PODA_JOBS") or 2)
        if price_in is None and cfg.get("PODA_PRICE_INPUT_PER_MTOK"):
            price_in = float(cfg["PODA_PRICE_INPUT_PER_MTOK"])
        if price_out is None and cfg.get("PODA_PRICE_OUTPUT_PER_MTOK"):
            price_out = float(cfg["PODA_PRICE_OUTPUT_PER_MTOK"])
    except ValueError as exc:
        console.print(f"[red]✗ Valor numérico no válido en la configuración: {exc}[/red]")
        raise typer.Exit(2)
    if (price_in is None) != (price_out is None):
        console.print("[red]✗ Indica las dos tarifas: --price-in y --price-out.[/red]")
        raise typer.Exit(2)
    llm = None
    if not no_llm:
        try:
            llm = resolve_llm(cfg, provider, model, triage_model, api_base)
        except ConfigError as exc:
            console.print(f"[red]✗ {exc}[/red]")
            raise typer.Exit(2)
        if not yes and sys.stdin.isatty() and console.is_terminal:
            llm = _confirm(console, cfg, llm, triage_model, api_base)
            if llm is None:
                raise typer.Exit(0)
    options = RunOptions(project=project, out=out, env_file=env_file, no_llm=no_llm, no_discover=no_discover,
                         dry_run=dry_run, resume=resume, force=force, min_confidence=min_confidence.value,
                         agent_mode=agent_mode.value, batch_size=batch_size, agent_batch_size=agent_batch_size,
                         jobs=jobs, max_candidates=max_candidates, max_turns=max_turns, timeout=timeout,
                         max_output_tokens=max_output_tokens, price_in=price_in, price_out=price_out,
                         exclude=exclude, sonar=sonar, sonar_project_key=sonar_project_key)
    bus = EventBus()
    ui = PlainUI(console) if plain or not console.is_terminal else RichUI(console)
    bus.subscribe(ui)
    pipeline = Pipeline(options, bus, cfg, llm)
    code = 2
    ui.start()
    try:
        code = pipeline.run()
    except PipelineError as exc:
        ui.stop()
        console.print(f"[red]✗ {exc}[/red]", markup=True)
        raise typer.Exit(2)
    except KeyboardInterrupt:
        ui.stop()
        console.print("[yellow]Interrumpido. Relanza con --resume para aprovechar las respuestas ya obtenidas.[/yellow]")
        raise typer.Exit(130)
    finally:
        ui.stop()
    raise typer.Exit(code)


@app.command()
def check(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=PROVIDER_HELP),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    triage_model: Optional[str] = typer.Option(None, "--triage-model"),
    api_base: Optional[str] = typer.Option(None, "--api-base"),
    env_file: Optional[Path] = typer.Option(None, "--env-file", exists=True, dir_okay=False, help="Fichero .env alternativo."),
    ping: bool = typer.Option(False, "--ping", help="Hace una llamada mínima al modelo para comprobar acceso y coste."),
) -> None:
    """Comprueba la configuración del proveedor y el modelo (sin secretos)."""
    console = Console(highlight=False)
    cfg = load_env(env_file)
    try:
        llm = resolve_llm(cfg, provider, model, triage_model, api_base)
    except ConfigError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(2)
    console.print(f"[green]✔[/green] {llm.display}\n  modelo LiteLLM: {llm.model}\n  triaje: {llm.triage_model}"
                  + (f"\n  api_base: {llm.api_base}" if llm.api_base else ""))
    if ping:
        from .core.llm_engine import LLMEngine, LLMCallError, LLMFatalError
        from .utils.metrics import MetricsTracker
        metrics = MetricsTracker()
        engine = LLMEngine(llm, metrics, max_output_tokens=20, timeout=60)
        try:
            reply = asyncio.run(engine.complete([{"role": "user", "content": "Reply with the single word OK."}],
                                                phase="check", label="check", mode="ping"))
        except (LLMFatalError, LLMCallError) as exc:
            console.print(f"[red]✗ {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✔[/green] respuesta: {reply.content.strip()[:60]!r} · tokens {reply.prompt_tokens}/"
                      f"{reply.completion_tokens} · coste {reply.cost_usd if reply.cost_usd is not None else 'n/d'}")


@app.command()
def models(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=PROVIDER_HELP),
    env_file: Optional[Path] = typer.Option(None, "--env-file", exists=True, dir_okay=False, help="Fichero .env alternativo."),
) -> None:
    """Lista los modelos disponibles para el proveedor."""
    console = Console(highlight=False)
    cfg = load_env(env_file)
    from .config import detect_provider
    try:
        chosen = detect_provider(cfg, provider, None)
    except ConfigError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(2)
    try:
        names = list_models(cfg, chosen)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ No se pudo listar los modelos de {chosen}: {exc}[/red]")
        raise typer.Exit(1)
    current = {"litellm": cfg.get("LITELLM_MODEL"), "copilot": cfg.get("COPILOT_MODEL")}.get(chosen)
    for name in names:
        console.print(("[green]●[/green] " if name == current else "  ") + name)
    if not names:
        console.print(f"Sin lista para '{chosen}': indica el modelo con --model (cadena de LiteLLM).")


@app.command()
def login(
    provider: str = typer.Argument("copilot", help="copilot (proxy local) o github_copilot (LiteLLM nativo)."),
    env_file: Optional[Path] = typer.Option(None, "--env-file", exists=True, dir_okay=False, help="Fichero .env alternativo."),
) -> None:
    """Inicia sesión en GitHub Copilot (device flow)."""
    cfg = load_env(env_file)
    if provider == "copilot":
        raise typer.Exit(subprocess.call([sys.executable, "-m", "poda.providers.copilot_proxy", "login"],
                                         env=copilot_env(cfg)))
    if provider == "github_copilot":
        from litellm.llms.github_copilot.authenticator import Authenticator
        Authenticator().get_api_key()
        typer.echo("Sesión de GitHub Copilot lista para LiteLLM.")
        return
    typer.echo(f"El proveedor '{provider}' no necesita login: configura sus credenciales en .env.")


@app.command()
def dashboard(
    host: Optional[str] = typer.Option(None, "--host", help="Interfaz en la que escucha (por defecto 127.0.0.1; 0.0.0.0 dentro de Docker)."),
    port: int = typer.Option(8770, "--port", help="Puerto local."),
    env_file: Optional[Path] = typer.Option(None, "--env-file", exists=True, dir_okay=False, help="Fichero .env alternativo."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Abrir el navegador al arrancar."),
    allow_host: List[str] = typer.Option([], "--allow-host", help="Nombre de host adicional permitido (repetible)."),
) -> None:
    """Panel web para conectar proveedores (LiteLLM, GitHub Copilot) y elegir el modelo de Poda AI."""
    import os
    import time
    import webbrowser
    from .dashboard.server import serve

    if env_file is not None:
        os.environ["PODA_ENV_FILE"] = str(env_file.resolve())
    in_container = Path("/.dockerenv").exists()
    if host is None:
        # Inside Docker the browser is on the host: listen on every interface (the port is
        # published only on the host's 127.0.0.1 by docker-compose.yml) and do not open a browser.
        host = "0.0.0.0" if in_container else "127.0.0.1"
    if in_container:
        open_browser = False
    console = Console(highlight=False)
    try:
        server = serve(host, port, extra_hosts=tuple(allow_host))
    except OSError as exc:
        console.print(f"[red]✗ No se pudo abrir el puerto {port}: {exc}[/red]")
        raise typer.Exit(2)
    console.print(Panel.fit(f"[bold]{server.url}[/bold]\n\nÁbrela en el navegador.\n"
                            "Ctrl+C para cerrar.", title="Poda AI · dashboard", border_style="cyan"))
    if open_browser and host in ("127.0.0.1", "localhost"):
        webbrowser.open(server.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        console.print("Dashboard cerrado.")


def main() -> None:
    app()
