"""Static analysis layer: runs every analyzer and merges their findings.

Order matters: junk is flagged first (so it is never used as evidence of use),
libraries are analysed before files (so classes that shadow a library class are
recognised), and duplicates found by several analyzers are merged at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..events import null_emit
from ..models import Finding, Findings
from .analyzers import ast_members
from .analyzers.comments import detect_commented_code
from .analyzers.dependencies import DependencyDetector
from .analyzers.junk import detect_junk
from .analyzers.reachability import detect_unused_code
from .analyzers.sonar import PODA_PROFILE, Sonar, SonarError, import_issues, project_key, summarize
from .inventory import Inventory, RefIndex


@dataclass
class StaticResult:
    index: Optional[RefIndex] = None
    jars_in_project: int = 0
    jars_used: int = 0
    archive_jars: int = 0
    sonar_note: Optional[str] = None
    merged: int = 0
    warnings: List[str] = field(default_factory=list)


def merge_duplicates(findings: Findings) -> int:
    """Merge overlapping line findings of the same category reported by several analyzers."""
    merged = 0
    seen: Dict[tuple, List[Finding]] = {}
    for f in findings:
        if f.status != "pending" or not f.start_line or f.category not in (
                "commented_code", "unused_import", "unused_member", "unreachable_code"):
            continue
        key = (f.category, f.path)
        target = next((g for g in seen.get(key, [])
                       if not (f.end_line < g.start_line or f.start_line > g.end_line)), None)
        if target is None:
            seen.setdefault(key, []).append(f)
            continue
        target.detectors = sorted(set(target.detectors + f.detectors))
        target.evidence = target.evidence + [e for e in f.evidence if e not in target.evidence]
        if not f.needs_llm and f.confidence == "high":
            target.confidence, target.needs_llm = "high", False
        f.status, f.status_detail = "superseded", f"duplicado de {target.id}"
        merged += 1
    return merged


class StaticAnalyzer:
    STEPS = ("basura y artefactos", "librerías y dependencias", "alcanzabilidad, miembros e imports",
             "código inalcanzable", "código comentado")

    def __init__(self, inv: Inventory, findings: Findings, cfg: Dict[str, str], emit=null_emit,
                 sonar: bool = False, sonar_project_key: Optional[str] = None,
                 work_dir: Optional[Path] = None) -> None:
        self.inv = inv
        self.findings = findings
        self.cfg = cfg
        self.emit = emit
        self.sonar = sonar or bool(sonar_project_key)
        self.sonar_project_key = sonar_project_key
        self.work_dir = work_dir or Path(".")

    def _step(self, number: int, text: str) -> None:
        self.emit("progress", phase="static", done=number - 1, total=len(self.STEPS))
        self.emit("activity", text, label="estático")

    def run(self) -> StaticResult:
        result = StaticResult()

        self._step(1, "buscando basura y artefactos de compilación")
        detect_junk(self.inv, self.findings)

        self._step(2, "analizando librerías (bytecode de los .jar) y dependencias declaradas")
        deps = DependencyDetector(self.inv, self.findings)
        deps.run()
        result.jars_in_project = len(deps.jars)
        result.jars_used = sum(1 for r in deps.used_jars if r in deps.jars)
        result.archive_jars = len(deps.archive_jars)

        self._step(3, "calculando qué ficheros, miembros e imports no alcanza ningún código vivo")
        result.index = detect_unused_code(self.inv, self.findings, deps.jar_classes)

        self._step(4, "buscando ramas y sentencias que nunca se ejecutan")
        ast_members.detect_unreachable(self.inv, self.findings)

        self._step(5, "buscando código comentado")
        detect_commented_code(self.inv, self.findings)

        self.emit("progress", phase="static", done=len(self.STEPS), total=len(self.STEPS))
        result.merged = merge_duplicates(self.findings)
        return result

    def run_sonar(self) -> Dict[str, object]:
        """SonarQube analysis (first phase). Raises SonarError when it cannot be done."""
        sonar = Sonar(self.cfg, self.work_dir / "sonar", self.emit)
        if not sonar.configured():
            raise SonarError("no hay ningún SonarQube configurado (SONAR_HOST_URL)")
        key = self.sonar_project_key or project_key(self.inv.root.name)
        if not self.sonar_project_key:
            exclusions = [f"{d}/**" for d in sorted(self.inv.excluded_dirs | set(self.inv.build_output_dirs))]
            exclusions += [f"{d}/**" for d, _ in self.inv.skipped]
            try:
                self.emit("activity", f"preparando el perfil de reglas «{PODA_PROFILE}»", label="sonar")
                active = sonar.prepare_profiles()
                sonar.bind_project(key, list(active))
                self.emit("detail", f"SonarQube: perfil «{PODA_PROFILE}» con las reglas de código sin uso · "
                          + ", ".join(f"{lang} {n}" for lang, n in sorted(active.items())))
            except SonarError as exc:
                self.emit("warn", f"No se pudo preparar el perfil «{PODA_PROFILE}» en SonarQube ({exc}): se usa el "
                                  "perfil por defecto, que puede no incluir las reglas de código sin uso.")
            task = sonar.scan(self.inv, key, self.inv.dominant_encoding(), exclusions)
            self.emit("activity", "esperando a que SonarQube procese el análisis", label="sonar")
            sonar.wait(task)
        self.emit("activity", "descargando las incidencias de código sin uso", label="sonar")
        issues = sonar.issues(key)
        stats = import_issues(issues, self.inv, self.findings)
        return summarize(issues, stats, key)
