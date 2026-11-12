"""Rewriter: decision policy and deterministic application. The LLM never edits code.

1. ``decide`` turns findings + verdicts into remove / keep / review, applying
   the confidence policy and the never-remove patterns of the project context.
2. ``Rewriter.apply`` hands the removals to the byte-exact applier (edits.py),
   which works on a copy and reverts any file that stops being valid.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..events import null_emit
from ..models import CONF_RANK, Finding
from .edits import Applier, ApplyStats
from .inventory import Inventory

REPORT_ONLY = ("info", "duplicate_library", "sonar_other")


def protected_by(path: str, patterns: Iterable[Dict[str, Any]]) -> Optional[str]:
    for item in patterns:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or item.get("path") or "").replace("\\", "/").strip()
        while pattern.startswith("./"):
            pattern = pattern[2:]
        pattern = pattern.lstrip("/")
        if not pattern:
            continue
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path + "/", pattern) or path == pattern.rstrip("/"):
            return f"{pattern} ({item.get('why') or ''})".strip()
    return None


def decide(findings: Iterable[Finding], min_conf: str, llm_used: bool,
           never_remove: Iterable[Dict[str, Any]] = ()) -> None:
    protect = list(never_remove)
    for f in findings:
        if f.status != "pending":
            continue
        if f.category in REPORT_ONLY:
            f.decision, f.status = "review", "reported"
            continue
        if f.edit and f.edit.get("op") in ("delete_path", "delete_paths") and f.category != "junk":
            rule = protected_by(f.path, protect)
            if rule:
                f.decision, f.status = "keep", "kept"
                f.status_detail = f"protegido por el contexto del proyecto: {rule}"
                continue
        if f.edit is None:
            f.decision, f.status = "review", "review"
            f.status_detail = f.status_detail or "sin eliminación automática posible: revisar a mano"
            continue
        if not f.needs_llm:
            if f.confidence == "high":
                f.decision = "remove"
            else:
                f.decision, f.status = "review", "review"
            continue
        verdict = f.llm
        if verdict is None:
            f.decision, f.status = "review", "review"
            f.status_detail = f.status_detail or ("sin verificar por IA (--no-llm)" if not llm_used
                                                  else "la IA no dio veredicto")
        elif verdict["decision"] == "remove" and CONF_RANK.get(verdict["confidence"], 0) >= CONF_RANK[min_conf]:
            f.decision = "remove"
        elif verdict["decision"] == "keep":
            f.decision, f.status = "keep", "kept"
            f.status_detail = verdict.get("reason", "")
        else:
            f.decision, f.status = "review", "review"
            f.status_detail = f"IA: {verdict['decision']} ({verdict['confidence']}): {verdict.get('reason', '')}"


class Rewriter:
    def __init__(self, inv: Inventory, code_dir: Path, emit=null_emit) -> None:
        self.applier = Applier(inv, code_dir, emit)

    def apply(self, findings: List[Finding], write: bool = True) -> ApplyStats:
        return self.applier.run(findings, write=write)
