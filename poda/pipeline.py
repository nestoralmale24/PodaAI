"""A poda run: eight phases over one project, reported through the event bus."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import LLMConfig, default_output_dir, load_env
from .core.inventory import Inventory
from .core.rewriter import Rewriter, decide
from .core.analyzers.sonar import SonarError
from .core.static_analyzer import StaticAnalyzer
from .events import EventBus
from .models import CATEGORIES, CONF_RANK, Findings
from .reporting.metrics_report import build_metrics, write_metrics
from .reporting.report import CATEGORY_LABEL, write_report
from .utils.formatting import fmt_int, fmt_secs, fmt_usd
from .utils.metrics import LOC_SCOPE, MetricsTracker, loc_stats, not_counted_text
from .utils.ui import FileLog

STAGE_ONE = ("unused_directory", "unused_file", "unused_library", "unused_dependency")
PHASE_TITLES = ["Inventario", "SonarQube", "Análisis estático", "Contexto del proyecto (IA)", "Verificación (IA)",
                "Descubrimiento (IA)", "Aplicación de cambios", "Informes"]
EXTRA_LABELS = {"duplicate_library": "Librerías duplicadas", "sonar_other": "Otras incidencias de SonarQube",
                "info": "Avisos"}


class PipelineError(RuntimeError):
    pass


class PipelineCancelled(PipelineError):
    """Stopped on request (dashboard "Detener"): the current step finishes, nothing else runs."""


@dataclass
class RunOptions:
    project: Path
    out: Optional[Path] = None
    env_file: Optional[Path] = None
    no_llm: bool = False
    no_discover: bool = False
    dry_run: bool = False
    resume: bool = False
    force: bool = False
    min_confidence: str = "high"
    agent_mode: str = "auto"
    batch_size: int = 15
    agent_batch_size: int = 5
    jobs: int = 2
    max_candidates: int = 0          # 0 = no cap (the dashboard always validates every finding)
    max_turns: Optional[int] = None
    timeout: int = 600
    max_output_tokens: int = 8000
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    exclude: List[str] = field(default_factory=list)
    sonar: bool = False
    sonar_project_key: Optional[str] = None
    sonar_required: bool = False   # the dashboard: no SonarQube, no run


def litellm_key_spend(llm: LLMConfig) -> Optional[float]:
    """Spend of the LiteLLM gateway key (best effort; None if the gateway does not allow it)."""
    if llm.provider != "litellm" or not llm.api_base or not llm.api_key:
        return None
    request = urllib.request.Request(llm.api_base.rstrip("/") + "/key/info",
                                     headers={"Authorization": f"Bearer {llm.api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    info = data.get("info") if isinstance(data, dict) and isinstance(data.get("info"), dict) else data
    spend = info.get("spend") if isinstance(info, dict) else None
    return float(spend) if isinstance(spend, (int, float)) else None


class Pipeline:
    TOTAL = len(PHASE_TITLES)

    def __init__(self, opts: RunOptions, bus: EventBus, cfg: Optional[Dict[str, str]] = None,
                 llm: Optional[LLMConfig] = None) -> None:
        self.opts = opts
        self.bus = bus
        self.emit = bus.emit
        self.cfg = cfg if cfg is not None else load_env(opts.env_file)
        self.llm = None if opts.no_llm else llm
        self.metrics = MetricsTracker()
        self.language = self.cfg.get("OUTPUT_LANGUAGE") or "Spanish"
        self.fatal: Optional[str] = None
        self.cancel = threading.Event()
        self.sonar_summary: Optional[Dict[str, Any]] = None

    # -- helpers ------------------------------------------------------------------------
    def _phase(self, index: int) -> str:
        if self.cancel.is_set():
            raise PipelineCancelled("Ejecución detenida a petición del usuario.")
        title = PHASE_TITLES[index - 1]
        self.metrics.phase_start(title)
        self.emit("phase_start", index=index, total=self.TOTAL, title=title)
        return title

    def _end(self, index: int, summary: str = "") -> None:
        title = PHASE_TITLES[index - 1]
        seconds = self.metrics.phase_end(title)
        self.emit("phase_end", index=index, title=title, seconds=seconds, summary=summary)

    def _skip(self, index: int, why: str) -> None:
        self.emit("phase_skip", why, index=index, title=PHASE_TITLES[index - 1])

    def prepare_output(self):
        project_dir = self.opts.project.resolve()
        if not project_dir.is_dir():
            raise PipelineError(f"No existe el directorio del proyecto: {project_dir}")
        out_dir = self.opts.out.resolve() if self.opts.out else default_output_dir(project_dir)
        if out_dir == project_dir or project_dir in out_dir.parents:
            raise PipelineError("La carpeta de salida no puede estar dentro del proyecto analizado.")
        work_dir = out_dir / ".poda"
        if out_dir.exists() and any(out_dir.iterdir()):
            if not work_dir.exists():
                raise PipelineError(f"{out_dir} existe y no es una salida de Poda AI: elige otra con --out.")
            if not (self.opts.force or self.opts.resume):
                raise PipelineError(f"{out_dir} ya existe. Usa --resume para reutilizar las respuestas de la IA "
                                    "o --force para empezar de cero.")
            if self.opts.force and not self.opts.resume:
                shutil.rmtree(out_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        return project_dir, out_dir, work_dir

    # -- run ----------------------------------------------------------------------------
    def run(self) -> int:
        project_dir, out_dir, work_dir = self.prepare_output()
        log_path = out_dir / "run.log"
        log = open(log_path, "w" if not self.opts.resume else "a", encoding="utf-8")
        self.bus.subscribe(FileLog(log))
        started = time.time()
        try:
            return self._run(project_dir, out_dir, work_dir, started)
        finally:
            log.close()

    def _run(self, project_dir: Path, out_dir: Path, work_dir: Path, started: float) -> int:
        opts = self.opts
        from . import __version__
        self.emit("header", f"Poda AI {__version__} · limpieza de código muerto")
        self.emit("info", f"Proyecto:  {project_dir}")
        self.emit("info", f"Salida:    {out_dir}")
        self.emit("info", f"LLM:       {self.llm.display if self.llm else 'desactivado (--no-llm)'}"
                  + (f" · triaje: {self.llm.triage_model}" if self.llm and self.llm.triage_model != self.llm.model else ""))
        self.emit("info", f"Modo:      {'simulación (--dry-run)' if opts.dry_run else 'copia limpia'} · "
                          f"confianza mínima {opts.min_confidence} · agente {opts.agent_mode}"
                          + (" · SonarQube" if opts.sonar or opts.sonar_project_key else "")
                          + ("" if self.llm else " · sin IA: solo informe, no se elimina nada"))

        # 1 · inventory
        self._phase(1)
        exclude_names = [x for x in (self.cfg.get("EXCLUDE_DIRS") or "").split(",") if x.strip()]
        globs = [g for item in opts.exclude for g in item.split(",") if g.strip()]
        inv = Inventory(project_dir, exclude_names, globs, out_dir=out_dir)
        loc_before = loc_stats(inv)
        for rel_dir, why in inv.skipped:
            self.emit("detail", f"no se analiza ni se copia: {rel_dir}/ ({why})")
        for rel_dir in sorted(inv.excluded_dirs):
            self.emit("detail", f"excluido del análisis (se copia tal cual): {rel_dir}/")
        kinds = Counter(f.kind for f in inv.files if not f.generated)
        excluded_text = not_counted_text(loc_before)
        if excluded_text:
            self.emit("detail", f"no cuentan como líneas de código: {excluded_text}")
        self._end(1, f"{fmt_int(len(inv.files))} ficheros · {fmt_int(loc_before['totals']['code'])} líneas de código "
                     f"({LOC_SCOPE}) · " + ", ".join(f"{k} {v}" for k, v in kinds.most_common(5)))

        findings = Findings()
        analyzer = StaticAnalyzer(inv, findings, self.cfg, self.emit, sonar=opts.sonar,
                                  sonar_project_key=opts.sonar_project_key, work_dir=work_dir)

        # 2 · SonarQube first: its detection and its summary
        if opts.sonar or opts.sonar_project_key or opts.sonar_required:
            self._phase(2)
            try:
                self.sonar_summary = analyzer.run_sonar()
            except SonarError as exc:
                if opts.sonar_required:
                    raise PipelineError(f"SonarQube no ha podido analizar la aplicación: {exc}") from exc
                self.emit("warn", f"SonarQube no disponible: {exc}")
                self._end(2, "no disponible")
            else:
                self.emit("info", f"🔬 SonarQube: {self.sonar_summary['text']}")
                self._end(2, self.sonar_summary["text"])
        else:
            self._skip(2, "omitido (SonarQube no configurado)")

        # 3 · Poda AI's own detectors: what SonarQube does not see
        self._phase(3)
        static = analyzer.run()
        # Nothing is removed without the AI: every candidate goes to the semantic layer.
        for f in findings:
            if f.edit:
                f.needs_llm = True
        if static.archive_jars or static.jars_in_project:
            self.emit("detail", f"{static.jars_in_project} .jar en el proyecto ({static.jars_used} en uso)"
                      + (f" · {static.archive_jars} .jar dentro de paquetes .war/.ear como referencia"
                         if static.archive_jars else ""))
        if static.merged:
            self.emit("detail", f"{static.merged} hallazgos duplicados entre analizadores fusionados")
        pending = [f for f in findings if f.status == "pending"]
        for category in CATEGORIES:
            items = [f for f in pending if f.category == category]
            if not items:
                continue
            verify = sum(1 for f in items if f.edit)
            from_sonar = sum(1 for f in items if any(d.startswith("sonar:") for d in f.detectors))
            label = CATEGORY_LABEL["es"].get(category) or EXTRA_LABELS.get(category, category)
            parts = [f"{verify} para la IA"] if verify else ["solo informe"]
            parts += [f"{from_sonar} de SonarQube"] if from_sonar else []
            self.emit("finding_summary", f"{label}: {len(items)} ({', '.join(parts)})",
                      category=category, count=len(items), auto=0, verify=verify)
        ai_total = sum(1 for f in pending if f.edit)
        self._end(3, f"{len(pending)} hallazgos · {ai_total} para validar con la IA")

        # 4-6 · semantic validation
        ctx: Dict[str, Any] = {}
        if self.llm is not None:
            ctx = asyncio.run(self._ai_phases(inv, findings, work_dir))
        else:
            for index in (4, 5, 6):
                self._skip(index, "omitido (--no-llm): sin IA no se elimina nada")

        # 7 · apply
        self._phase(7)
        decide(findings, opts.min_confidence, self.llm is not None, ctx.get("never_remove") or [])
        code_dir = out_dir / project_dir.name
        rewriter = Rewriter(inv, code_dir, self.emit)
        stats = rewriter.apply(list(findings), write=not opts.dry_run)
        loc_after = loc_stats(Inventory(code_dir)) if not opts.dry_run and code_dir.exists() else None
        review = sum(1 for f in findings if f.status in ("review", "failed"))
        self._end(7, f"{fmt_int(stats.files_deleted)} ficheros eliminados · {fmt_int(stats.lines_removed)} líneas "
                     f"quitadas en {stats.files_modified} ficheros · {review} para revisión manual")

        # 8 · reports
        self._phase(8)
        finished = time.time()
        llm_info = None
        if self.llm is not None:
            llm_info = {"provider": self.llm.provider, "model": self.llm.model, "triage_model": self.llm.triage_model}
        metrics = build_metrics(self.metrics, project=project_dir.name, llm=llm_info, started=started,
                                finished=finished, loc_before=loc_before, loc_after=loc_after, stats=stats,
                                findings=list(findings), dry_run=opts.dry_run)
        metrics_json, metrics_md = write_metrics(out_dir, metrics)
        report, findings_json = write_report(out_dir, {
            "project": project_dir.name, "project_dir": str(project_dir), "code_dir": str(code_dir),
            "findings": list(findings), "stats": stats, "llm": self.llm.display if self.llm else "—",
            "metrics": metrics, "language": self.language, "dry_run": opts.dry_run, "no_llm": self.llm is None,
            "skipped": inv.skipped, "started": started, "finished": finished,
            "sonar": self.sonar_summary["text"] if self.sonar_summary else static.sonar_note,
            "warnings": static.warnings + ([f"La IA se detuvo por un error del proveedor: {self.fatal[:300]}"]
                                           if self.fatal else [])})
        for path in (report, metrics_md, metrics_json, findings_json, out_dir / "run.log"):
            self.emit("detail", f"📄 {path}")
        self._end(8)
        cost = metrics["cost"]
        loc = metrics["loc"]
        lines = [f"✅ Terminado en {fmt_secs(time.time() - started)}",
                 f"   🗑  {fmt_int(stats.files_deleted)} ficheros eliminados ({stats.dirs_deleted} directorios) · "
                 f"✂  {fmt_int(stats.lines_removed)} líneas quitadas en {stats.files_modified} ficheros",
                 f"   🔍 {review} elementos para revisión manual (poda_report.md)"]
        if loc.get("reduced"):
            lines.append(f"   📉 líneas de código: {fmt_int(loc['before']['code'])} → {fmt_int(loc['after']['code'])} "
                         f"(−{loc['reduced_pct']['code']} %)")
        tokens = self.metrics.tokens()
        lines.append(f"   💰 coste: {fmt_usd(cost['total_usd']) if cost['total_usd'] is not None else (cost.get('note') or 'n/d')}"
                     f" · tokens {fmt_int(tokens['prompt'])} entrada / {fmt_int(tokens['completion'])} salida")
        if not opts.dry_run:
            lines.append(f"   📦 código limpio: {code_dir}")
        self.emit("done", "\n".join(lines))
        return 1 if self.fatal else 0

    async def _ai_phases(self, inv: Inventory, findings: Findings, work_dir: Path) -> Dict[str, Any]:
        from .core.llm_engine import LLMEngine
        from .core.semantic import SemanticOptions, SemanticValidator
        from .core.tools import ReadOnlyTools

        opts = self.opts
        assert self.llm is not None
        spend_before = litellm_key_spend(self.llm)
        engine = LLMEngine(self.llm, self.metrics, self.emit, cache_path=work_dir / "llm-cache.json",
                           resume=opts.resume, max_output_tokens=opts.max_output_tokens, timeout=opts.timeout,
                           price_in=opts.price_in, price_out=opts.price_out, language=self.language)
        engine.cancel = self.cancel
        tools = ReadOnlyTools(inv)
        for f in findings:
            if f.status == "pending" and f.edit and f.edit.get("op") == "delete_path":
                tools.flagged.update([x.rel for x in inv.under(f.path)] if f.path in inv.dirs else [f.path])
        options = SemanticOptions(agent_mode=opts.agent_mode, batch_size=opts.batch_size,
                                  agent_batch_size=opts.agent_batch_size, jobs=max(1, opts.jobs))
        if opts.max_turns:
            options.max_turns_agent = options.max_turns_context = options.max_turns_discover = opts.max_turns
        validator = SemanticValidator(engine, tools, inv, findings, project_name=inv.root.name,
                                      language=self.language, options=options, emit=self.emit)
        if not engine.tools_supported():
            self.emit("warn", "el modelo no admite herramientas: solo modo snippet y sin descubrimiento")

        # 4 · context
        self._phase(4)
        summary = "\n".join(f"- Candidates to verify: {c} = {n}" for c, n in
                            Counter(f.category for f in findings if f.status == "pending").most_common())
        if self.sonar_summary:
            summary += "\n- SonarQube (rules tagged `unused`): " + "; ".join(
                f"{r['rule']} {r['count']}" for r in self.sonar_summary["by_rule"]) if self.sonar_summary["by_rule"] \
                else "\n- SonarQube found no dead-code issues."
        ctx = await validator.project_context(inv.summary_markdown() + "\n" + summary)
        if ctx:
            self.emit("info", f"🧭 {str(ctx.get('stack_summary', ''))[:240]}")
            self._end(4, f"{len(ctx.get('entry_points') or [])} puntos de entrada · "
                         f"{len(ctx.get('wiring_mechanisms') or [])} mecanismos de conexión · "
                         f"{len(ctx.get('never_remove') or [])} patrones protegidos")
        else:
            self.emit("warn", "no se obtuvo el contexto del proyecto: la verificación sigue sin él")
            self._end(4, "sin contexto")

        # 5 · verification (files and libraries first; then what survives inside files)
        self._phase(5)
        pending = [f for f in findings if f.status == "pending" and f.needs_llm and f.llm is None and f.edit]
        if opts.max_candidates and len(pending) > opts.max_candidates:      # 0 = no cap: the AI checks them all
            ordered = [f for f in pending if f.category in STAGE_ONE] + [f for f in pending if f.category not in STAGE_ONE]
            for f in ordered[opts.max_candidates:]:
                f.status, f.status_detail = "review", "no verificado: supera el límite --max-candidates"
            self.emit("warn", f"{len(pending) - opts.max_candidates} hallazgos superan --max-candidates y quedan para revisión")
        stage1 = [f for f in findings if f.status == "pending" and f.needs_llm and f.llm is None and f.edit
                  and f.category in STAGE_ONE]
        self.emit("detail", f"etapa 1: {len(stage1)} ficheros, directorios, librerías y dependencias")
        counts = await validator.verify(stage1)
        doomed = {f.path for f in findings if f.edit and f.edit.get("op") == "delete_path" and (
            (f.llm and f.llm["decision"] == "remove"
             and CONF_RANK.get(f.llm["confidence"], 0) >= CONF_RANK[opts.min_confidence])
            or (not f.needs_llm and f.confidence == "high"))}
        stage2 = []
        for f in findings:
            if f.status != "pending" or not f.needs_llm or f.llm is not None or not f.edit or f.category in STAGE_ONE:
                continue
            if any(f.path == d or f.path.startswith(d + "/") for d in doomed):
                f.status, f.status_detail = "superseded", "el fichero completo se elimina"
                continue
            stage2.append(f)
        self.emit("detail", f"etapa 2: {len(stage2)} miembros, imports y bloques de código comentado")
        counts.update(await validator.verify(stage2))
        self._end(5, f"{counts['remove'] + counts['agente_remove']} eliminar · "
                     f"{counts['keep'] + counts['agente_keep']} conservar · {counts['escalate']} escalados al agente")

        # 6 · discovery
        if opts.no_discover:
            self._skip(6, "omitido (--no-discover)")
        elif engine.fatal:
            self._skip(6, "omitido por el error del proveedor")
        else:
            self._phase(6)
            added = await validator.discover()
            self._end(6, f"{added} hallazgos nuevos del agente")

        spend_after = litellm_key_spend(self.llm)
        if spend_before is not None and spend_after is not None:
            self.metrics.gateway_spend = {"before": spend_before, "after": spend_after,
                                          "delta": round(spend_after - spend_before, 6)}
        self.fatal = engine.fatal
        return ctx
