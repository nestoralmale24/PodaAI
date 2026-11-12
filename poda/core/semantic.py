"""Semantic validation layer: project context, verification (snippet → agent) and discovery."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..events import null_emit
from ..models import Finding, Findings
from ..prompts import render
from ..schemas import DiscoveryBatch, DiscoveryItem, ProjectContext, VerdictBatch, schema_text
from ..utils.text import split_lines, strip_eol
from .agent import run_agent
from .evidence import EvidenceBuilder
from .inventory import Inventory
from .llm_engine import LLMCallError, LLMEngine, LLMFatalError
from .tools import ReadOnlyTools

SNIPPET_ONLY = {"commented_code", "junk", "unreachable_code"}
DISCOVERY_CATEGORIES = {"unreachable_code", "disabled_feature", "dead_config", "unused_member", "leftover",
                        "unused_file", "unused_directory"}
PROTECTED_NAMES = {"pom.xml", "build.xml", "build.gradle", "settings.gradle", "package.json", "web.xml",
                   "application.xml", "faces-config.xml", "struts-config.xml", "makefile", "global.asax",
                   "web.config", "app.config"}


@dataclass
class SemanticOptions:
    agent_mode: str = "auto"        # auto | always | never
    batch_size: int = 15            # candidates per snippet call
    agent_batch_size: int = 5       # candidates per agent run
    jobs: int = 2
    max_turns_agent: int = 25
    max_turns_context: int = 30
    max_turns_discover: int = 30
    discover_scopes: int = 6
    discover_findings: int = 25


def _chunks(items: List[Finding], size: int) -> List[List[Finding]]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


class SemanticValidator:
    def __init__(self, engine: LLMEngine, tools: ReadOnlyTools, inv: Inventory, findings: Findings, *,
                 project_name: str, language: str, options: SemanticOptions, emit=null_emit) -> None:
        self.engine = engine
        self.tools = tools
        self.inv = inv
        self.findings = findings
        self.project_name = project_name
        self.language = language
        self.options = options
        self.emit = emit
        self.system = render("system", {"OUTPUT_LANGUAGE": language})
        self.evidence = EvidenceBuilder(inv, tools)
        self.context: Dict[str, Any] = {}

    # -- helpers ------------------------------------------------------------------------
    def context_json(self, limit: int = 12000) -> str:
        if not self.context:
            return '{"note": "No project context available: investigate the wiring yourself."}'
        data = dict(self.context)
        text = json.dumps(data, ensure_ascii=False, indent=1)
        for key in ("entry_points", "never_remove", "wiring_mechanisms", "runtime_or_external"):
            while len(text) > limit and isinstance(data.get(key), list) and len(data[key]) > 5:
                data[key] = data[key][: max(5, len(data[key]) * 2 // 3)]
                text = json.dumps(data, ensure_ascii=False, indent=1)
        return text[:limit]

    def _messages(self, task: str) -> List[Dict[str, Any]]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": task}]

    @property
    def mode(self) -> str:
        return self.options.agent_mode if self.engine.tools_supported() else "never"

    # -- phase: project context ---------------------------------------------------------
    async def project_context(self, summary: str) -> Dict[str, Any]:
        task = render("project_context", {"PROJECT_NAME": self.project_name, "INVENTORY_SUMMARY": summary,
                                          "SCHEMA": schema_text(ProjectContext)})
        try:
            if self.mode == "never":
                result = await self.engine.judge(self._messages(task), ProjectContext, phase="context",
                                                 label="contexto", mode="snippet")
            else:
                result = await run_agent(self.engine, self.tools, system=self.system, task=task,
                                         schema=ProjectContext, phase="context", label="contexto",
                                         max_turns=self.options.max_turns_context, emit=self.emit)
        except (LLMFatalError, LLMCallError):
            result = None
        self.context = result.model_dump() if result else {}
        return self.context

    # -- phase: verification --------------------------------------------------------------
    async def verify(self, candidates: List[Finding]) -> Counter:
        counts: Counter = Counter()
        if not candidates:
            return counts
        mode = self.mode
        semaphore = asyncio.Semaphore(max(1, self.options.jobs))
        agent_queue: List[Finding] = []
        snippet_list: List[Finding] = []
        for f in candidates:
            (agent_queue if mode == "always" and f.category not in SNIPPET_ONLY else snippet_list).append(f)

        snippet_batches = _chunks(snippet_list, self.options.batch_size)
        progress = {"done": 0, "total": len(snippet_batches)}

        def advance() -> None:
            progress["done"] += 1
            self.emit("progress", phase="verify", done=progress["done"], total=max(1, progress["total"]))

        async def snippet(number: int, batch: List[Finding]) -> None:
            label = f"snippet {number}/{len(snippet_batches)}"
            task = render("verify_snippet", {
                "PROJECT_NAME": self.project_name, "CONTEXT_JSON": self.context_json(),
                "BATCH_LABEL": f"batch {number} of {len(snippet_batches)}, {len(batch)} candidates",
                "CANDIDATES_JSON": json.dumps([self.evidence.pack(f) for f in batch], ensure_ascii=False, indent=1),
                "SCHEMA": schema_text(VerdictBatch)})
            async with semaphore:
                self.emit("activity", f"valida {len(batch)} hallazgos con su evidencia", label=label)
                try:
                    result = await self.engine.judge(self._messages(task), VerdictBatch, phase="verify",
                                                     label=label, mode="snippet", model=self.engine.cfg.triage_model)
                except (LLMFatalError, LLMCallError):
                    result = None
            verdicts = {v.id: v for v in (result.verdicts if result else [])}
            local: Counter = Counter()
            for f in batch:
                v = verdicts.get(f.id)
                escalate = mode != "never" and f.category not in SNIPPET_ONLY and (
                    v is None or v.decision in ("needs_context", "unsure")
                    or (v.decision == "remove" and v.confidence != "high"))
                if v is not None:
                    f.llm = {**v.model_dump(), "mode": "snippet"}
                    local[v.decision] += 1
                else:
                    local["sin respuesta"] += 1
                if escalate:
                    agent_queue.append(f)
                    local["escalate"] += 1
            counts.update(local)
            self.emit("verdicts", label=label, remove=local["remove"], keep=local["keep"],
                      unsure=local["unsure"] + local["needs_context"] + local["sin respuesta"],
                      escalate=local["escalate"])
            advance()

        await asyncio.gather(*(snippet(i, b) for i, b in enumerate(snippet_batches, start=1)))
        if self.engine.fatal or not agent_queue:
            return counts
        self.engine.metrics.escalations += len(agent_queue)
        agent_batches = _chunks(agent_queue, self.options.agent_batch_size)
        progress["total"] += len(agent_batches)

        async def agent(number: int, batch: List[Finding]) -> None:
            label = f"agente {number}/{len(agent_batches)}"
            task = render("verify_agent", {
                "PROJECT_NAME": self.project_name, "CONTEXT_JSON": self.context_json(),
                "BATCH_LABEL": f"batch {number} of {len(agent_batches)}, {len(batch)} candidates",
                "CANDIDATES_JSON": json.dumps([self.evidence.pack(f) for f in batch], ensure_ascii=False, indent=1),
                "SCHEMA": schema_text(VerdictBatch)})
            async with semaphore:
                try:
                    result = await run_agent(
                        self.engine, self.tools, system=self.system, task=task, schema=VerdictBatch,
                        phase="verify", label=label, max_turns=self.options.max_turns_agent, emit=self.emit,
                        pending_rule="Include every candidate id exactly once: " + ", ".join(f.id for f in batch))
                except (LLMFatalError, LLMCallError):
                    result = None
            verdicts = {v.id: v for v in (result.verdicts if result else [])}
            local: Counter = Counter()
            for f in batch:
                v = verdicts.get(f.id)
                if v is not None:
                    f.llm = {**v.model_dump(), "mode": "agent"}
                    local[v.decision] += 1
                else:
                    local["sin respuesta"] += 1
            counts.update({f"agente_{k}": n for k, n in local.items()})
            self.emit("verdicts", label=label, remove=local["remove"], keep=local["keep"],
                      unsure=local["unsure"] + local["needs_context"] + local["sin respuesta"], escalate=0)
            advance()

        await asyncio.gather(*(agent(i, b) for i, b in enumerate(agent_batches, start=1)))
        return counts

    # -- phase: discovery ------------------------------------------------------------------
    def scopes(self) -> List[str]:
        live = [f.rel for f in self.inv.files if f.kind in ("source", "web", "config", "script", "db")
                and not f.excluded and not f.generated and f.rel not in self.inv.junk]
        if len(live) <= 400:
            return ["."]
        scopes = Counter(rel.split("/", 1)[0] for rel in live if "/" in rel)
        for _ in range(4):
            big = [s for s, n in scopes.items() if n > 800]
            if not big:
                break
            for scope in big:
                subs = Counter("/".join(rel.split("/")[:scope.count("/") + 2]) for rel in live
                               if rel.startswith(scope + "/") and rel.count("/") > scope.count("/") + 1)
                if len(subs) < 2:
                    continue
                del scopes[scope]
                scopes.update(subs)
        return [s for s, _ in scopes.most_common(self.options.discover_scopes)]

    def known(self, scope: str, limit: int = 150) -> str:
        lines = []
        prefix = "" if scope == "." else scope.rstrip("/") + "/"
        for f in self.findings:
            if f.category in ("info", "duplicate_library") or f.status == "superseded":
                continue
            if prefix and not (f.path + "/").startswith(prefix):
                continue
            if f.llm and f.llm.get("decision") == "keep":
                continue
            lines.append(f"- {f.location()}{' ' + f.symbol if f.symbol else ''} ({f.category})")
            if len(lines) >= limit:
                lines.append("- ... (more items omitted)")
                break
        return "\n".join(lines) or "- (none)"

    async def discover(self) -> int:
        if self.mode == "never":
            self.emit("detail", "descubrimiento omitido: el modelo no admite herramientas")
            return 0
        scopes = self.scopes()
        semaphore = asyncio.Semaphore(max(1, self.options.jobs))
        added = 0

        async def run(scope: str) -> None:
            nonlocal added
            label = f"descubre {scope}"
            task = render("discover", {
                "PROJECT_NAME": self.project_name, "CONTEXT_JSON": self.context_json(), "SCOPE": scope,
                "MAX_FINDINGS": str(self.options.discover_findings), "KNOWN_FINDINGS": self.known(scope),
                "SCHEMA": schema_text(DiscoveryBatch)})
            async with semaphore:
                try:
                    result = await run_agent(self.engine, self.tools, system=self.system, task=task,
                                             schema=DiscoveryBatch, phase="discover", label=label,
                                             max_turns=self.options.max_turns_discover, emit=self.emit,
                                             pending_rule="Report only what you already verified "
                                                          "(an empty list is fine).")
                except (LLMFatalError, LLMCallError):
                    result = None
            for item in (result.findings if result else [])[: self.options.discover_findings]:
                finding = self.to_finding(item)
                if finding is None or self._overlaps(finding):
                    continue
                self.findings.add(finding)
                added += 1
                self.emit("info", f"➕ {finding.location()}: {finding.reason[:100]}", label=label)

        await asyncio.gather(*(run(scope) for scope in scopes))
        return added

    def _overlaps(self, new: Finding) -> bool:
        for f in self.findings:
            if f.edit is None or f.status == "superseded":
                continue
            if f.edit.get("op") == "delete_path" and (new.path == f.path or new.path.startswith(f.path + "/")):
                return True
            if f.path == new.path and new.start_line and f.start_line:
                if not (new.end_line < f.start_line or new.start_line > (f.end_line or f.start_line)):
                    return True
        return False

    def to_finding(self, item: DiscoveryItem) -> Optional[Finding]:
        path = item.path.replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        path = path.strip("/")
        if not path:
            return None
        category = item.category if item.category in DISCOVERY_CATEGORIES else "leftover"
        llm = {"decision": item.decision, "confidence": item.confidence, "reason": item.reason[:500],
               "evidence": item.evidence, "mode": "agent"}
        reason = item.reason[:500] or "detectado por el agente"
        if path in self.inv.dirs and not item.start_line:
            if any(path == d or path.startswith(d + "/") for d in self.inv.excluded_dirs):
                return None
            finding = Finding(category="unused_directory", subtype="llm_discovery", path=path, reason=reason,
                              confidence=item.confidence, needs_llm=True, detectors=["llm:discovery"],
                              evidence=item.evidence, edit={"op": "delete_path"}, llm=llm)
            finding.removed_files = len(self.inv.under(path))
            return finding
        fi = self.inv.by_rel.get(path)
        if fi is None or fi.excluded or fi.generated:
            return None
        if not item.start_line:
            if fi.name.lower() in PROTECTED_NAMES or fi.kind == "build":
                llm["decision"] = "unsure"
                llm["reason"] = reason + " (descriptor o fichero de build: no se borra automáticamente)"
            return Finding(category="unused_file", subtype="llm_discovery", path=path, reason=reason,
                           confidence=item.confidence, needs_llm=True, detectors=["llm:discovery"],
                           evidence=item.evidence, edit={"op": "delete_path"}, llm=llm)
        start, end = item.start_line, item.end_line or item.start_line
        lines = split_lines(self.inv.text(fi) or "") if self.inv.is_text(fi) else []
        if not (1 <= start <= end <= len(lines)):
            return None
        snippet = "\n".join(strip_eol(x) for x in lines[start - 1:min(end, start + 9)])
        return Finding(category="discovered", subtype=category, path=path, start_line=start, end_line=end,
                       symbol=item.symbol, reason=reason, confidence=item.confidence, needs_llm=True,
                       detectors=["llm:discovery"], evidence=item.evidence, snippet=snippet, llm=llm,
                       edit={"op": "delete_lines", "ranges": [[start, end]],
                             "expect": {"first": item.first_line_text or "", "last": item.last_line_text or ""}})
