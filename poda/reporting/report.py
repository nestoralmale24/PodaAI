"""poda_report.md (what was removed and why) and findings.json (full audit)."""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import __version__
from ..core.edits import ApplyStats
from ..models import CATEGORIES, Finding
from ..utils.formatting import fmt_int, fmt_secs, fmt_usd

TEXT = {
    "es": {
        "title": "Limpieza de código muerto", "date": "Fecha", "project": "Proyecto analizado",
        "clean": "Código limpio", "llm": "Proveedor / modelo", "duration": "Duración",
        "cost": "Coste", "estimate": "estimación", "summary": "Resumen", "category": "Categoría", "detected": "Detectados",
        "removed": "Eliminados", "kept": "Conservados", "review": "A revisar", "how": "Cómo se ha decidido",
        "removed_h": "Eliminado", "files_h": "Ficheros y directorios", "deps_h": "Librerías y dependencias",
        "members_h": "Código sin uso dentro de ficheros", "comments_h": "Código comentado",
        "junk_h": "Basura y artefactos de compilación", "review_h": "Pendiente de revisión manual",
        "kept_h": "Conservado tras la verificación", "warnings_h": "Avisos", "outputs_h": "Ficheros generados",
        "path": "Ruta", "reason": "Motivo", "verification": "Verificación", "location": "Ubicación",
        "element": "Elemento", "file": "Fichero", "blocks": "Bloques", "lines": "Líneas", "type": "Tipo",
        "why_not": "Por qué no se ha eliminado", "more": "y {n} más en `findings.json`", "none": "Nada.",
        "det": "determinista", "ai": "IA", "dry": "Simulación (--dry-run): no se ha escrito el código limpio.",
        "no_llm": "Sin IA (--no-llm): solo se han aplicado los hallazgos deterministas de confianza alta.",
    },
    "en": {
        "title": "Dead-code cleanup", "date": "Date", "project": "Analysed project",
        "clean": "Clean code", "llm": "Provider / model", "duration": "Duration",
        "cost": "Cost", "estimate": "estimate", "summary": "Summary", "category": "Category", "detected": "Detected",
        "removed": "Removed", "kept": "Kept", "review": "To review", "how": "How decisions were made",
        "removed_h": "Removed", "files_h": "Files and directories", "deps_h": "Libraries and dependencies",
        "members_h": "Unused code inside files", "comments_h": "Commented-out code",
        "junk_h": "Junk and build leftovers", "review_h": "Pending manual review",
        "kept_h": "Kept after verification", "warnings_h": "Warnings", "outputs_h": "Generated files",
        "path": "Path", "reason": "Reason", "verification": "Verification", "location": "Location",
        "element": "Element", "file": "File", "blocks": "Blocks", "lines": "Lines", "type": "Type",
        "why_not": "Why it was not removed", "more": "and {n} more in `findings.json`", "none": "Nothing.",
        "det": "deterministic", "ai": "AI", "dry": "Dry run (--dry-run): the clean code was not written.",
        "no_llm": "No AI (--no-llm): only high-confidence deterministic findings were applied.",
    },
}

CATEGORY_LABEL = {
    "es": {"junk": "Basura y artefactos de compilación", "unused_directory": "Directorios sin uso",
           "unused_file": "Ficheros sin uso", "unused_library": "Librerías incluidas sin uso",
           "unused_dependency": "Dependencias declaradas sin uso",
           "unused_member": "Métodos, funciones y campos sin uso", "unused_import": "Imports sin uso",
           "unreachable_code": "Código inalcanzable (condiciones siempre falsas, código tras return)",
           "commented_code": "Código comentado",
           "discovered": "Hallazgos del agente (configuración muerta, funcionalidades desactivadas...)"},
    "en": {"junk": "Junk and build leftovers", "unused_directory": "Unused directories",
           "unused_file": "Unused files", "unused_library": "Unused bundled libraries",
           "unused_dependency": "Unused declared dependencies",
           "unused_member": "Unused methods, functions and fields", "unused_import": "Unused imports",
           "unreachable_code": "Unreachable code (always-false conditions, code after return)",
           "commented_code": "Commented-out code",
           "discovered": "Agent findings (dead configuration, disabled features...)"},
}

HOW = {
    "es": [
        "**1. SonarQube primero**: analiza la aplicación y resume las incidencias de código sin uso (código comentado, imports, miembros privados, variables, código inalcanzable…).",
        "**2. Análisis propio de Poda AI**: añade lo que SonarQube no ve: basura y restos de compilación, ficheros y directorios que nada alcanza, métodos públicos sin uso, librerías por bytecode, dependencias declaradas y ramas con condición siempre falsa.",
        "**3. Nada se elimina sin la IA**: cada hallazgo, venga de SonarQube o del análisis propio, lo valida el modelo con sus prompts: primero con el paquete de evidencia (modo snippet) y, si no basta o el riesgo lo exige, con un agente que busca en el código con herramientas de solo lectura. Solo se aplican los veredictos *eliminar* con la confianza mínima exigida; el resto queda para revisión manual.",
        "**4. El modelo nunca escribe código**: las eliminaciones las aplica Poda AI sobre una copia, línea a línea y sin cambiar la codificación; cada fichero editado se valida (sintaxis con tree-sitter, XML, JSON, llaves) y se revierte si empeora.",
    ],
    "en": [
        "**1. SonarQube first**: it analyses the application and summarises the unused-code issues (commented-out code, imports, private members, variables, unreachable code…).",
        "**2. Poda AI's own analysis**: adds what SonarQube does not see: junk and build leftovers, files and directories nothing reaches, unused public methods, libraries through bytecode, declared dependencies and always-false branches.",
        "**3. Nothing is removed without the AI**: every finding, from SonarQube or from the own analysis, is validated by the model with its prompts: first from the evidence pack (snippet mode) and, when that is not enough or the risk requires it, by an agent that searches the code with read-only tools. Only *remove* verdicts with the required confidence are applied; the rest is left for manual review.",
        "**4. The model never writes code**: Poda AI applies the removals to a copy, line by line and without changing the encoding; every edited file is validated (tree-sitter syntax, XML, JSON, braces) and reverted if it gets worse.",
    ],
}

CONFIDENCE = {"es": {"high": "alta", "medium": "media", "low": "baja"},
              "en": {"high": "high", "medium": "medium", "low": "low"}}


def _lang(language: str) -> str:
    return "en" if (language or "").lower().startswith("en") else "es"


def fmt_bytes(size: int) -> str:
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.1f} KB"


def _md(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _verification(f: Finding, lang: str) -> str:
    t = TEXT[lang]
    if f.llm:
        conf = CONFIDENCE[lang].get(f.llm.get("confidence"), f.llm.get("confidence"))
        mode = f.llm.get("mode")
        tag = f"{t['ai']}{' · ' + mode if mode else ''}"
        return f"{tag} ({conf}): {_md(f.llm.get('reason'))[:220]}"
    return f"{t['det']} ({', '.join(f.detectors) or t['det']})"


def _table(header: List[str], rows: List[List[str]], limit: int, lang: str) -> List[str]:
    t = TEXT[lang]
    if not rows:
        return [t["none"], ""]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(row) + " |" for row in rows[:limit]]
    if len(rows) > limit:
        out += ["", t["more"].format(n=len(rows) - limit)]
    return out + [""]


def write_report(out_dir: Path, ctx: Dict[str, Any]) -> Tuple[Path, Path]:
    """ctx: project, project_dir, code_dir, findings, stats, llm, metrics, language, dry_run, no_llm,
    skipped, started, finished, sonar, warnings."""
    lang = _lang(ctx.get("language", "Spanish"))
    t = TEXT[lang]
    labels = CATEGORY_LABEL[lang]
    findings: List[Finding] = ctx["findings"]
    stats: ApplyStats = ctx["stats"]
    metrics: Dict[str, Any] = ctx["metrics"]
    report_path = out_dir / "poda_report.md"
    findings_path = out_dir / "findings.json"
    findings_path.write_text(json.dumps({
        "tool": "poda", "version": __version__, "project": ctx["project"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "findings": [f.to_dict() for f in findings],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    cost = metrics["cost"]
    cost_line = (fmt_usd(cost["total_usd"]) if cost.get("total_usd") is not None else (cost.get("note") or "n/d"))
    if cost.get("total_usd") is not None and "copilot_estimate" in str(cost.get("source") or ""):
        cost_line += f" ({t['estimate']})"
    lines: List[str] = [f"# {t['title']}: {ctx['project']}", "", "| | |", "|---|---|",
                        f"| {t['date']} | {time.strftime('%Y-%m-%d %H:%M', time.localtime(ctx['started']))} |",
                        f"| {t['project']} | `{ctx['project_dir']}` |", f"| {t['clean']} | `{ctx['code_dir']}` |",
                        f"| {t['llm']} | {_md(ctx.get('llm') or '—')} |",
                        f"| {t['duration']} | {fmt_secs(ctx['finished'] - ctx['started'])} |",
                        f"| {t['cost']} | {_md(cost_line)} · `execution_metrics.md` |", ""]
    if ctx.get("dry_run"):
        lines += [f"> {t['dry']}", ""]
    if ctx.get("no_llm"):
        lines += [f"> {t['no_llm']}", ""]

    countable = [f for f in findings if f.category in labels and f.status != "superseded"]
    removed = [f for f in countable if f.status == "removed"]
    review = [f for f in countable if f.status in ("review", "failed")]
    kept = [f for f in countable if f.status == "kept"]
    loc = metrics.get("loc") or {}
    lines += [f"## {t['summary']}", ""]
    if lang == "es":
        lines.append(f"Se han eliminado **{fmt_int(stats.files_deleted)} ficheros** ({stats.dirs_deleted} directorios "
                     f"completos) y **{fmt_int(stats.lines_removed)} líneas** en {stats.files_modified} ficheros.")
        if loc.get("reduced"):
            lines.append(f"Líneas de código (sin comentarios, líneas en blanco, documentación ni metadatos del IDE): "
                         f"{fmt_int(loc['before']['code'])} → "
                         f"{fmt_int(loc['after']['code'])} (−{loc['reduced_pct']['code']} %).")
        lines.append(f"Quedan **{len(review)}** elementos pendientes de revisión manual.")
    else:
        lines.append(f"Removed **{fmt_int(stats.files_deleted)} files** ({stats.dirs_deleted} whole directories) and "
                     f"**{fmt_int(stats.lines_removed)} lines** in {stats.files_modified} files.")
        if loc.get("reduced"):
            lines.append(f"Lines of code (no comments, blank lines, documentation or IDE metadata): "
                         f"{fmt_int(loc['before']['code'])} → "
                         f"{fmt_int(loc['after']['code'])} (−{loc['reduced_pct']['code']} %).")
        lines.append(f"**{len(review)}** items are pending manual review.")
    if stats.bytes_before and stats.bytes_after:
        saved = 100.0 * (stats.bytes_before - stats.bytes_after) / stats.bytes_before
        lines.append(f"{'Tamaño' if lang == 'es' else 'Size'}: {fmt_bytes(stats.bytes_before)} → "
                     f"{fmt_bytes(stats.bytes_after)} (−{saved:.1f} %).")
    lines.append("")
    rows = []
    for category in CATEGORIES:
        items = [f for f in countable if f.category == category]
        if category in labels and items:
            rows.append([labels[category], str(len(items)), str(sum(1 for f in items if f.status == "removed")),
                         str(sum(1 for f in items if f.status == "kept")),
                         str(sum(1 for f in items if f.status in ("review", "failed")))])
    lines += _table([t["category"], t["detected"], t["removed"], t["kept"], t["review"]], rows, 50, lang)
    lines += [f"## {t['how']}", ""] + [f"- {x}" for x in HOW[lang]]
    if ctx.get("sonar"):
        lines.append(f"- **SonarQube**: {ctx['sonar']}")
    lines.append("")

    lines += [f"## {t['removed_h']}", "", f"### {t['files_h']}", ""]
    rows = [[f"`{f.path}{'/' if f.category == 'unused_directory' else ''}`", _md(f.reason), _verification(f, lang)]
            for f in removed if f.category in ("unused_directory", "unused_file")]
    lines += _table([t["path"], t["reason"], t["verification"]], rows, 80, lang)
    lines += [f"### {t['deps_h']}", ""]
    rows = [[f"`{_md(f.symbol or f.path)}`", f"`{f.location()}`", _verification(f, lang)]
            for f in removed if f.category in ("unused_library", "unused_dependency")]
    lines += _table([t["element"], t["location"], t["verification"]], rows, 60, lang)
    lines += [f"### {t['members_h']}", ""]
    rows = [[f"`{f.location()}`", f"`{_md(f.symbol)}`" if f.symbol else _md(f.subtype), _md(f.reason),
             _verification(f, lang)]
            for f in removed if f.category in ("unused_member", "unused_import", "unreachable_code", "discovered")]
    lines += _table([t["location"], t["element"], t["reason"], t["verification"]], rows, 80, lang)
    lines += [f"### {t['comments_h']}", ""]
    per_file: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for f in removed:
        if f.category == "commented_code":
            per_file[f.path][0] += 1
            per_file[f.path][1] += f.removed_lines
    rows = [[f"`{p}`", str(v[0]), str(v[1])] for p, v in sorted(per_file.items(), key=lambda kv: -kv[1][1])]
    lines += _table([t["file"], t["blocks"], t["lines"]], rows, 40, lang)
    lines += [f"### {t['junk_h']}", ""]
    rows = [[f"`{f.path}`", _md(f.reason) + (f" ({f.removed_files})" if f.removed_files > 1 else "")]
            for f in removed if f.category == "junk"]
    lines += _table([t["path"], t["reason"]], rows, 40, lang)

    lines += [f"## {t['review_h']}", ""]
    rows = [[f"`{f.location()}`", labels.get(f.category, f.category),
             _md(f.status_detail or (f.llm or {}).get("reason") or f.reason)[:260]] for f in review]
    lines += _table([t["location"], t["type"], t["why_not"]], rows, 100, lang)
    lines += [f"## {t['kept_h']}", ""]
    rows = [[f"`{f.location()}`", labels.get(f.category, f.category),
             _md(f.status_detail or (f.llm or {}).get("reason"))[:260]] for f in kept]
    lines += _table([t["location"], t["type"], t["reason"]], rows, 60, lang)

    warnings: List[str] = list(ctx.get("warnings") or [])
    for f in findings:
        if f.category in ("info", "duplicate_library"):
            extra = f" ({', '.join(f.evidence[:3])})" if f.category == "duplicate_library" else ""
            warnings.append(f"`{f.path}`: {_md(f.reason)}{extra}")
    for rule, count in Counter(f.subtype for f in findings if f.category == "sonar_other").most_common():
        warnings.append(f"SonarQube `{rule}`: {count} incidencias sin eliminación automática (ver `findings.json`)"
                        if lang == "es" else f"SonarQube `{rule}`: {count} issues without automatic removal")
    for rel_dir, why in ctx.get("skipped") or []:
        warnings.append(f"`{rel_dir}/` no se copia: {why}" if lang == "es" else f"`{rel_dir}/` not copied: {why}")
    for rel in stats.reverted:
        warnings.append(f"`{rel}`: cambios revertidos porque el fichero dejaba de ser válido" if lang == "es"
                        else f"`{rel}`: changes reverted because the file stopped being valid")
    lines += [f"## {t['warnings_h']}", ""] + ([f"- {w}" for w in warnings] or [t["none"]]) + [""]
    lines += [f"## {t['outputs_h']}", "", f"- `{ctx['code_dir']}/`",
              "- `poda_report.md`, `findings.json`, `execution_metrics.json`, `execution_metrics.md`, `run.log`", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path, findings_path
