"""Optional SonarQube integration.

Runs sonar-scanner on the original project (or reuses an existing analysis)
and imports the issues tagged `unused`, which is how SonarQube tags its
dead-code rules in every language (commented-out code, unused imports,
unused private members, dead stores, unreachable code...).

Removable rules are turned into findings with exact line ranges; the rest are
listed in the report for manual review.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from ..inventory import Inventory
from ...models import Finding, Findings
from ...utils.text import SYNTAXES, block_safe, classify_lines, split_lines, strip_eol
from ...events import null_emit
from .reachability import extract_members

RULE_LABELS = {
    "S125": "código comentado", "S1128": "imports sin uso", "S1144": "métodos privados sin uso",
    "S1068": "campos privados sin uso", "S3985": "clases privadas sin uso", "S1481": "variables locales sin uso",
    "S1172": "parámetros sin uso", "S1854": "asignaciones que no se usan", "S2589": "condiciones siempre iguales",
    "S1763": "código inalcanzable", "AvoidCommentedOutCodeCheck": "código comentado (HTML/JSP)",
}
REMOVABLE = {"S125": "commented_code", "AvoidCommentedOutCodeCheck": "commented_code", "S1128": "unused_import", "S1144": "unused_member",
             "S1068": "unused_member", "S3985": "unused_member"}


# Quality profile Poda AI creates in SonarQube: every rule tagged `unused`, in every language that has
# them. Recent SonarQube versions ship a default profile ("Sonar way core") without these rules.
PODA_PROFILE = "Poda AI (código sin uso)"
PAGE_SIZE = 500
MAX_RESULTS = 10_000   # SonarQube never returns more than this per search
FILE_BATCH = 50


class SonarError(RuntimeError):
    pass


class Sonar:
    def __init__(self, env: Dict[str, str], work_dir: Path, emit=null_emit) -> None:
        self.url = (env.get("SONAR_HOST_URL") or "").rstrip("/")
        self.token = env.get("SONAR_TOKEN") or ""
        self.scanner = env.get("SONAR_SCANNER") or ""
        self.emit = emit
        self.work_dir = work_dir

    def configured(self) -> bool:
        return bool(self.url)

    # -- HTTP -----------------------------------------------------------------
    def _post(self, path: str, params: Dict[str, object]) -> dict:
        return self._get(path, params, method="POST")

    def _get(self, path: str, params: Dict[str, object], method: str = "GET") -> dict:
        url = f"{self.url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        if self.token:
            basic = base64.b64encode(f"{self.token}:".encode()).decode()
            request.add_header("Authorization", f"Basic {basic}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}  # some POSTs answer 204 without a body
        except urllib.error.HTTPError as exc:
            raise SonarError(f"SonarQube respondió HTTP {exc.code} en {path}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SonarError(f"No se pudo consultar SonarQube ({path}): {exc}") from exc

    # -- dead-code rules --------------------------------------------------------------
    def prepare_profiles(self) -> Dict[str, int]:
        """Create or update the Poda AI profile in every language with `unused` rules. {language: rules}."""
        data = self._get("/api/rules/search", {"tags": "unused", "ps": 1, "facets": "languages"})
        languages = [value["val"] for facet in data.get("facets", []) if facet.get("property") == "languages"
                     for value in facet.get("values", []) if value.get("count")]
        active: Dict[str, int] = {}
        for language in languages:
            found = self._get("/api/qualityprofiles/search", {"language": language}).get("profiles") or []
            profile = next((p for p in found if p.get("name") == PODA_PROFILE), None)
            key = profile["key"] if profile else \
                self._post("/api/qualityprofiles/create", {"language": language, "name": PODA_PROFILE})["profile"]["key"]
            self._post("/api/qualityprofiles/activate_rules", {"targetKey": key, "tags": "unused", "languages": language})
            active[language] = int(self._get("/api/rules/search", {"qprofile": key, "activation": "true",
                                                                     "ps": 1}).get("total") or 0)
        return active

    def bind_project(self, key: str, languages: List[str]) -> None:
        """Create the project (if needed) and analyse it with the Poda AI profile."""
        try:
            self._post("/api/projects/create", {"project": key, "name": key})
        except SonarError as exc:
            if "HTTP 400" not in str(exc):  # 400: the project already exists
                raise
        for language in languages:
            self._post("/api/qualityprofiles/add_project",
                       {"language": language, "qualityProfile": PODA_PROFILE, "project": key})

    # -- scanner ----------------------------------------------------------------
    def scanner_command(self) -> Optional[List[str]]:
        if self.scanner:
            return shlex.split(self.scanner)
        found = shutil.which("sonar-scanner")
        return [found] if found else None

    def scan(self, inv: Inventory, key: str, encoding: str, exclusions: List[str]) -> str:
        cmd = self.scanner_command()
        if not cmd:
            raise SonarError("No se encuentra sonar-scanner: viene en la imagen de Poda AI "
                             "(reconstrúyela con docker compose up -d --build) o define SONAR_SCANNER")
        work = self.work_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        # The legacy app may ship a sonar-project.properties that narrows sources or adds exclusions:
        # point the scanner to an empty settings file so that the whole application is analysed.
        no_settings = work / "poda-empty.properties"
        no_settings.write_text("# Poda AI: se ignora el sonar-project.properties de la aplicación\n", encoding="utf-8")
        binaries = self._java_binaries(inv)
        args = cmd + [
            f"-Dsonar.projectKey={key}", f"-Dsonar.projectName={key}",
            f"-Dsonar.projectBaseDir={inv.root}", "-Dsonar.sources=.",
            f"-Dsonar.working.directory={work}", f"-Dsonar.sourceEncoding={encoding}",
            "-Dsonar.scm.disabled=true", f"-Dsonar.java.binaries={binaries}",
            f"-Dproject.settings={no_settings}",
        ]
        if exclusions:
            args.append("-Dsonar.exclusions=" + ",".join(exclusions))
        env = dict(os.environ)
        env["SONAR_HOST_URL"] = self.url
        if self.token:
            env["SONAR_TOKEN"] = self.token
        self.emit("detail", f"sonar-scanner sobre {inv.root} (clave {key})")
        process = subprocess.Popen(args, cwd=str(inv.root), env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.emit("activity", line[:180], label="sonar")
        if process.wait() != 0:
            raise SonarError(f"sonar-scanner terminó con código {process.returncode}")
        report = work / "report-task.txt"
        if not report.exists():
            raise SonarError("sonar-scanner no generó report-task.txt")
        props = dict(line.split("=", 1) for line in report.read_text(encoding="utf-8").splitlines() if "=" in line)
        task = props.get("ceTaskId")
        if not task:
            raise SonarError("report-task.txt sin ceTaskId")
        return task

    def wait(self, task_id: str, timeout: int = 900) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self._get("/api/ce/task", {"id": task_id}).get("task", {})
            status = task.get("status")
            if status == "SUCCESS":
                return
            if status in ("FAILED", "CANCELED"):
                why = f": {task['errorMessage']}" if task.get("errorMessage") else ""
                raise SonarError(f"El procesado del análisis en SonarQube terminó en {status}{why}")
            time.sleep(3)
        raise SonarError("Tiempo de espera agotado esperando a SonarQube")

    def issues(self, key: str) -> List[dict]:
        """Every open issue tagged `unused`. SonarQube answers at most 10,000 results per query:
        beyond that the issues are fetched by batches of files (split again while a batch is too big)."""
        first, total = self._issues_page(key, 1)
        if total <= MAX_RESULTS:
            return self._issues_all(key, first, total)
        files = self._file_keys(key)
        self.emit("detail", f"SonarQube: {total} incidencias de código sin uso; como pasan del límite de "
                            f"{MAX_RESULTS} por consulta, se descargan por lotes de ficheros ({len(files)} ficheros)")
        out: List[dict] = []
        for start in range(0, len(files), FILE_BATCH):
            out.extend(self._issues_for(files[start:start + FILE_BATCH]))
        return out

    def _issues_page(self, components: str, page: int):
        data = self._get("/api/issues/search", {"componentKeys": components, "tags": "unused",
                                                "resolved": "false", "ps": PAGE_SIZE, "p": page})
        total = data.get("paging", {}).get("total", data.get("total", 0)) or 0
        return data.get("issues", []), int(total)

    def _issues_all(self, components: str, first: List[dict], total: int) -> List[dict]:
        out = list(first)
        page = 1
        while page * PAGE_SIZE < min(total, MAX_RESULTS):
            page += 1
            more, _ = self._issues_page(components, page)
            if not more:
                break
            out.extend(more)
        return out

    def _issues_for(self, files: List[str]) -> List[dict]:
        components = ",".join(files)
        first, total = self._issues_page(components, 1)
        if total > MAX_RESULTS and len(files) > 1:
            half = len(files) // 2
            return self._issues_for(files[:half]) + self._issues_for(files[half:])
        if total > MAX_RESULTS:
            self.emit("warn", f"SonarQube: {files[0]} tiene {total} incidencias de código sin uso; solo se "
                              f"pueden descargar las {MAX_RESULTS} primeras")
        return self._issues_all(components, first, total)

    def _file_keys(self, key: str) -> List[str]:
        keys: List[str] = []
        page = 1
        while True:
            data = self._get("/api/components/tree", {"component": key, "qualifiers": "FIL",
                                                      "ps": PAGE_SIZE, "p": page})
            keys.extend(c["key"] for c in data.get("components", []) if c.get("key"))
            total = int(data.get("paging", {}).get("total", 0) or 0)
            if not data.get("components") or page * PAGE_SIZE >= total:
                return keys
            page += 1

    @staticmethod
    def _java_binaries(inv: Inventory) -> str:
        for candidate in ("WebContent/WEB-INF/classes", "src/main/webapp/WEB-INF/classes", "target/classes",
                          "build/classes", "bin", "WebRoot/WEB-INF/classes"):
            path = inv.root / candidate
            if path.is_dir():
                return str(path)
        return "."


def project_key(name: str) -> str:
    return "poda-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-").lower()


def import_issues(issues: List[dict], inv: Inventory, findings: Findings) -> Dict[str, int]:
    stats = {"removable": 0, "reported": 0, "ignored": 0}
    members_cache: Dict[str, list] = {}
    for issue in issues:
        rule = issue.get("rule", "")
        rule_id = rule.split(":")[-1]
        component = issue.get("component", "")
        path = component.split(":", 1)[1] if ":" in component else component
        fi = inv.by_rel.get(path)
        if fi is None or fi.excluded or fi.generated:
            stats["ignored"] += 1
            continue
        rng = issue.get("textRange") or {}
        start = rng.get("startLine") or issue.get("line")
        end = rng.get("endLine") or start
        message = issue.get("message", "")
        category = REMOVABLE.get(rule_id)
        finding = None
        if category and start:
            text = inv.text(fi) or ""
            lines = split_lines(text)
            if category == "commented_code" and fi.syntax in SYNTAXES:
                cl = classify_lines(lines, SYNTAXES[fi.syntax])
                if all(cl.lines[i - 1].kind == "comment" for i in range(start, end + 1)) \
                        and block_safe(cl, start, end):
                    finding = Finding(category="commented_code", subtype="sonar_commented_code", path=path,
                                      start_line=start, end_line=end, reason=f"{end - start + 1} línea(s) de código comentado",
                                      confidence="high", needs_llm=False, detectors=[f"sonar:{rule}"],
                                      evidence=[message], snippet="\n".join(strip_eol(x) for x in lines[start - 1:end][:12]),
                                      edit={"op": "delete_lines", "ranges": [[start, end]]})
            elif category == "unused_import" and start == end and re.match(
                    r"^\s*(import|using|from)\b", strip_eol(lines[start - 1]) if start <= len(lines) else ""):
                finding = Finding(category="unused_import", subtype="unused_import", path=path,
                                  start_line=start, end_line=end, symbol=strip_eol(lines[start - 1]).strip(),
                                  reason="import que no se usa", confidence="high", needs_llm=False,
                                  detectors=[f"sonar:{rule}"], evidence=[message],
                                  edit={"op": "delete_lines", "ranges": [[start, start]]})
            elif category == "unused_member":
                if path not in members_cache:
                    members_cache[path] = extract_members(inv, fi)
                member = next((m for m in members_cache[path] if m.decl_line == start), None)
                if member:
                    finding = Finding(category="unused_member", subtype=f"private_{member.kind}", path=path,
                                      symbol=member.name, start_line=member.start_line, end_line=member.end_line,
                                      reason="miembro privado que no se usa", confidence="high", needs_llm=False,
                                      detectors=[f"sonar:{rule}"], evidence=[message],
                                      snippet="\n".join(strip_eol(x) for x in lines[member.start_line - 1:member.end_line][:10]),
                                      edit={"op": "delete_lines", "ranges": [[member.start_line, member.end_line]]})
        if finding:
            findings.add(finding)
            stats["removable"] += 1
        else:
            findings.add(Finding(category="sonar_other", subtype=rule, path=path, start_line=start,
                                 end_line=end, reason=message or rule, confidence="high", needs_llm=False,
                                 detectors=[f"sonar:{rule}"], evidence=[]))
            stats["reported"] += 1
    return stats


def summarize(issues: List[dict], stats: Dict[str, int], key: str) -> Dict[str, object]:
    """What SonarQube found, grouped by rule, for the run log, the report and the AI context."""
    counts: Dict[str, int] = {}
    for issue in issues:
        rule_id = str(issue.get("rule", "")).split(":")[-1]
        counts[rule_id] = counts.get(rule_id, 0) + 1
    by_rule = [{"rule": rule, "label": RULE_LABELS.get(rule, f"regla {rule}"), "count": n}
               for rule, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    text = f"{len(issues)} incidencias de código sin uso"
    if by_rule:
        text += ": " + ", ".join(f"{r['label']} {r['count']}" for r in by_rule[:8])
    return {"project_key": key, "total": len(issues), "by_rule": by_rule, "text": text, **stats}
