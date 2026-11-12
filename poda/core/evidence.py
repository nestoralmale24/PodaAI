"""Evidence packs: what the static layer can tell the LLM about each candidate.

The pack lets the snippet mode decide without tools: every occurrence of the
candidate's names in the project (tagged when the occurrence is in
documentation, generated code or another removal candidate), the declaration
and its surroundings, and the analyzers' own findings.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..models import Finding
from ..utils.text import split_lines, strip_eol
from .inventory import Inventory
from .tools import ReadOnlyTools

VERSION_SUFFIX = re.compile(r"[-_]\d[\w.\-]*$")
HEAD_EXT = {"java", "cs", "py", "js", "ts", "vb", "bas", "cls", "frm", "php", "kt", "scala", "groovy"}


def _bean_property(name: str) -> str:
    m = re.match(r"^(get|set|is)([A-Z]\w*)$", name)
    return m.group(2)[0].lower() + m.group(2)[1:] if m else ""


class EvidenceBuilder:
    def __init__(self, inv: Inventory, tools: ReadOnlyTools, max_hits: int = 12) -> None:
        self.inv = inv
        self.tools = tools
        self.max_hits = max_hits

    def terms(self, f: Finding) -> List[str]:
        name = f.path.rsplit("/", 1)[-1]
        if f.category == "unused_file":
            return [name.rsplit(".", 1)[0] if "." in name else name]
        if f.category == "unused_directory":
            return [name]
        if f.category in ("unused_member", "unused_import") and f.symbol:
            names = [s.strip().split(".")[-1] for s in f.symbol.split(",") if s.strip()]
            return names + [p for p in (_bean_property(n) for n in names) if p]
        if f.category == "unused_library" and f.symbol:
            stem = f.symbol[:-4] if f.symbol.lower().endswith(".jar") else f.symbol
            return [VERSION_SUFFIX.sub("", stem)]
        if f.category == "unused_dependency" and f.symbol:
            parts = f.symbol.split(":")
            term = parts[1] if len(parts) >= 2 else parts[0]
            return [term] if " " not in term else []
        return []

    def occurrences(self, f: Finding) -> Dict[str, Any]:
        terms = [t for t in self.terms(f) if len(t) >= 2]
        if not terms:
            return {}
        pattern = "|".join(re.escape(t) for t in terms)
        regex = re.compile(r"(?<![\w$])(?:" + pattern + r")(?![\w$])", re.I)
        exclude = f.path if f.category == "unused_file" else None
        hits, total = self.tools.search(regex, limit=self.max_hits, exclude=exclude)
        shown = [f"{fi.rel}:{line}: {text}{self.tools.tag(fi)}" for fi, line, text in hits]
        return {"searched": terms, "total": total, "shown": shown}

    def _context_lines(self, f: Finding, before: int = 3, after: int = 3) -> str:
        fi = self.inv.by_rel.get(f.path)
        if fi is None or not f.start_line or not self.inv.is_text(fi):
            return ""
        lines = split_lines(self.inv.text(fi) or "")
        start = max(1, f.start_line - before)
        end = min(len(lines), (f.end_line or f.start_line) + after, start + 40)
        return "\n".join(f"{n:5d}  {strip_eol(lines[n - 1])}" for n in range(start, end + 1))

    def _head(self, f: Finding, count: int = 25) -> str:
        fi = self.inv.by_rel.get(f.path)
        if fi is None or fi.ext not in HEAD_EXT or not self.inv.is_text(fi):
            return ""
        lines = split_lines(self.inv.text(fi) or "")[:count]
        return "\n".join(strip_eol(line) for line in lines)[:1800]

    def pack(self, f: Finding) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"id": f.id, "type": f.category, "subtype": f.subtype, "path": f.path}
        if f.start_line:
            payload["lines"] = f"{f.start_line}-{f.end_line or f.start_line}"
        if f.symbol:
            payload["symbol"] = f.symbol
        payload["static_confidence"] = f.confidence
        payload["detectors"] = f.detectors          # "sonar:java:S1144" = found by SonarQube
        payload["reason"] = f.reason
        payload["deterministic_evidence"] = f.evidence[:6]
        occurrences = self.occurrences(f)
        if occurrences:
            payload["occurrences"] = occurrences
        if f.category in ("commented_code", "unused_member", "unused_import", "discovered", "unreachable_code"):
            context = self._context_lines(f)
            if context:
                payload["code_with_context"] = context
        elif f.snippet:
            payload["snippet"] = f.snippet
        if f.category == "unused_file":
            head = self._head(f)
            if head:
                payload["file_head"] = head
        return payload
