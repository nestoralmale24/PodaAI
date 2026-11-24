"""execution_metrics.json and execution_metrics.md."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..models import Finding
from ..providers import copilot_pricing
from ..utils.formatting import fmt_int, fmt_secs, fmt_usd
from ..utils.metrics import LOC_SCOPE, MetricsTracker, not_counted_text

SOURCE_LABEL = {
    "litellm": "tabla de precios de LiteLLM", "gateway": "coste devuelto por el gateway LiteLLM",
    "litellm_key_spend": "gasto de la clave en el gateway LiteLLM", "price_table": "tarifas --price-in/--price-out",
    "claude_subscription": "plan de Claude (sin coste por token)",
    "copilot_subscription": "GitHub Copilot, sin tarifa publicada para el modelo",
    "copilot_estimate": "estimación con las tarifas de GitHub Copilot", "unknown": "desconocido",
}


def _loc_delta(before: Dict[str, Any], after: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not after:
        return {"before": before["totals"], "after": None, "reduced": None, "scope": LOC_SCOPE,
                "not_counted": before.get("not_counted") or {}}
    reduced = {k: before["totals"][k] - after["totals"][k] for k in ("files", "physical", "code")}
    pct = {k: round(100.0 * reduced[k] / before["totals"][k], 2) if before["totals"][k] else 0.0
           for k in ("physical", "code")}
    by_language = {}
    for lang, values in before["by_language"].items():
        later = after["by_language"].get(lang, {"files": 0, "physical": 0, "code": 0})
        by_language[lang] = {"before": values, "after": later,
                             "reduced_code": values["code"] - later["code"],
                             "reduced_physical": values["physical"] - later["physical"]}
    return {"before": before["totals"], "after": after["totals"], "reduced": reduced, "reduced_pct": pct,
            "by_language": by_language, "scope": LOC_SCOPE, "not_counted": before.get("not_counted") or {}}


def build_metrics(tracker: MetricsTracker, *, project: str, llm: Optional[Dict[str, Any]], started: float,
                  finished: float, loc_before: Dict[str, Any], loc_after: Optional[Dict[str, Any]], stats,
                  findings: List[Finding], dry_run: bool) -> Dict[str, Any]:
    summary = tracker.summary()
    cost = summary["cost_usd"]
    source = ", ".join(summary["cost_sources"]) or None
    note = None
    provider = (llm or {}).get("provider")
    spend = tracker.gateway_spend
    if provider == "litellm" and spend and spend.get("delta") is not None:
        cost, source = round(spend["delta"], 6), "litellm_key_spend"
    copilot = provider in ("copilot", "github_copilot")
    if copilot and "copilot_estimate" in summary["cost_sources"]:
        note = copilot_pricing.note(summary["calls_without_cost"])
    elif copilot and summary["calls"] and source in (None, "copilot_subscription"):
        cost, note = None, ("GitHub Copilot no publica tarifa para este modelo: consulta el consumo de créditos "
                            "en tu cuenta de GitHub")
    elif provider == "claude" and summary["calls"]:
        cost, note = None, "Claude se usa con tu plan (Pro/Max): no hay coste por token, cuenta contra su límite de uso"
    elif cost is None and summary["calls"]:
        note = ("El proveedor no devolvió coste y LiteLLM no conoce este modelo: usa --price-in/--price-out "
                "(USD por millón de tokens) para estimarlo")
    elif not summary["calls"]:
        cost, note = 0.0, "sin llamadas al modelo"
    statuses = Counter((f.category, f.status) for f in findings if f.status != "superseded")
    by_category: Dict[str, Dict[str, int]] = {}
    for (category, status), count in statuses.items():
        by_category.setdefault(category, {})[status] = count
    return {
        "tool": "poda", "version": __version__, "project": project, "dry_run": dry_run,
        "timing": {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(finished)),
            "wall_seconds": round(finished - started, 2),
            "phases_seconds": {name: p.get("seconds") for name, p in tracker.phases.items()},
        },
        "llm": {**(llm or {"provider": None}), **{k: v for k, v in summary.items() if k != "detail"}},
        "cost": {"total_usd": cost, "source": source, "note": note, "gateway_spend": spend},
        "loc": _loc_delta(loc_before, loc_after),
        "changes": {"files_deleted": stats.files_deleted, "directories_deleted": stats.dirs_deleted,
                    "files_modified": stats.files_modified, "lines_removed": stats.lines_removed,
                    "bytes_before": stats.bytes_before, "bytes_after": stats.bytes_after,
                    "reverted_files": stats.reverted},
        "findings": by_category,
        "llm_calls": summary["detail"],
    }


def write_metrics(out_dir: Path, data: Dict[str, Any]) -> Tuple[Path, Path]:
    json_path = out_dir / "execution_metrics.json"
    md_path = out_dir / "execution_metrics.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    llm, cost, loc, changes = data["llm"], data["cost"], data["loc"], data["changes"]
    rows = [
        ("Duración total", fmt_secs(data["timing"]["wall_seconds"])),
        ("Proveedor / modelo", f"{llm.get('provider') or '—'} · {llm.get('model') or '—'}"
         + (f" (triaje: {llm['triage_model']})" if llm.get("triage_model") and llm.get("triage_model") != llm.get("model") else "")),
        ("Llamadas al modelo", f"{llm.get('calls', 0)}" + (f" ({llm['failed_calls']} fallidas)" if llm.get("failed_calls") else "")
         + (f" · {llm['reused_results']} resultados reutilizados" if llm.get("reused_results") else "")),
        ("Casos escalados al agente", fmt_int(llm.get("escalations_to_agent", 0))),
        ("Herramientas usadas por el agente", fmt_int(llm.get("tool_calls", 0))),
        ("Tokens de entrada / salida", f"{fmt_int(llm.get('prompt_tokens', 0))} / {fmt_int(llm.get('completion_tokens', 0))}"),
        ("Tokens servidos desde caché", fmt_int(llm.get("cached_tokens", 0))),
        ("Coste", (fmt_usd(cost["total_usd"]) if cost["total_usd"] is not None else "n/d")
         + (f" ({', '.join(SOURCE_LABEL.get(s.strip(), s) for s in cost['source'].split(','))})" if cost.get("source") else "")),
    ]
    if cost.get("note"):
        rows.append(("Nota", cost["note"]))
    if loc.get("reduced"):
        rows += [
            (f"Líneas de código ({LOC_SCOPE})",
             f"{fmt_int(loc['before']['code'])} → {fmt_int(loc['after']['code'])} "
             f"(−{fmt_int(loc['reduced']['code'])}, −{loc['reduced_pct']['code']} %)"),
            ("Líneas físicas", f"{fmt_int(loc['before']['physical'])} → {fmt_int(loc['after']['physical'])} "
                               f"(−{fmt_int(loc['reduced']['physical'])}, −{loc['reduced_pct']['physical']} %)"),
        ]
    if not loc.get("reduced") and loc.get("before"):
        rows.append((f"Líneas de código ({LOC_SCOPE})", fmt_int(loc["before"].get("code", 0))))
    if loc.get("not_counted"):
        rows.append(("No cuenta como código (no se toca)", not_counted_text(loc)))
    rows.append(("Ficheros eliminados / modificados", f"{fmt_int(changes['files_deleted'])} / {fmt_int(changes['files_modified'])}"))
    out = [f"# Métricas de ejecución de Poda AI: {data['project']}", "", "| | |", "|---|---|"]
    out += [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows] + [""]
    out += ["## Tiempo por fase", "", "| Fase | Duración |", "|---|---:|"]
    out += [f"| {name} | {fmt_secs(secs or 0)} |" for name, secs in data["timing"]["phases_seconds"].items()] + [""]
    if loc.get("by_language"):
        out += ["## Líneas de código por lenguaje", "", "| Lenguaje | Antes | Después | Reducción |", "|---|---:|---:|---:|"]
        langs = sorted(loc["by_language"].items(), key=lambda kv: -kv[1]["reduced_code"])
        for lang, values in langs[:20]:
            out.append(f"| {lang} | {fmt_int(values['before']['code'])} | {fmt_int(values['after']['code'])} | "
                       f"{fmt_int(values['reduced_code'])} |")
        out.append("")
    by_model = llm.get("by_model") or {}
    if by_model:
        out += ["## Uso por modelo", "", "| Modelo | Llamadas | Tokens entrada | Tokens salida | Coste |", "|---|---:|---:|---:|---:|"]
        for model, values in by_model.items():
            out.append(f"| {model} | {values['calls']} | {fmt_int(values['prompt_tokens'])} | "
                       f"{fmt_int(values['completion_tokens'])} | {fmt_usd(values['cost_usd']) if values['cost_usd'] else 'n/d'} |")
        out.append("")
    md_path.write_text("\n".join(out), encoding="utf-8")
    return json_path, md_path
