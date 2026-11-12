"""Finding model shared by detectors, the LLM phases, the applier and the reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

CONF_RANK = {"low": 0, "medium": 1, "high": 2}

# Category -> id prefix. The order is also the order used in reports.
CATEGORIES = {
    "junk": "JNK",
    "unused_directory": "DIR",
    "unused_file": "FIL",
    "unused_library": "LIB",
    "unused_dependency": "DEP",
    "unused_member": "MEM",
    "unused_import": "IMP",
    "unreachable_code": "UNR",
    "commented_code": "CMT",
    "discovered": "DSC",
    "duplicate_library": "DUP",
    "sonar_other": "SNR",
    "info": "INF",
}


@dataclass
class Finding:
    category: str
    subtype: str
    path: str                      # posix path relative to the project root
    reason: str                    # short human explanation (report language)
    confidence: str = "medium"     # deterministic confidence: high | medium | low
    needs_llm: bool = True         # must be verified by the LLM before removal
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    symbol: Optional[str] = None
    detectors: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    snippet: Optional[str] = None
    # How to remove it:
    #   {"op": "delete_path"}
    #   {"op": "delete_lines", "ranges": [[s, e], ...], "expect": {"first": "...", "last": "..."},
    #    "strip_trailing_comma": [line, ...]}
    #   None: report only
    edit: Optional[Dict[str, Any]] = None
    llm: Optional[Dict[str, Any]] = None
    decision: str = "pending"      # remove | keep | review
    status: str = "pending"        # removed | kept | review | failed | superseded | reported
    status_detail: str = ""
    id: str = ""
    removed_lines: int = 0
    removed_files: int = 0

    def location(self) -> str:
        if self.start_line:
            if self.end_line and self.end_line != self.start_line:
                return f"{self.path}:{self.start_line}-{self.end_line}"
            return f"{self.path}:{self.start_line}"
        return self.path

    def line_count(self) -> int:
        if self.edit and self.edit.get("op") == "delete_lines":
            return sum(e - s + 1 for s, e in self.edit.get("ranges", []))
        return 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Findings:
    """Ordered collection that assigns stable ids per category."""

    def __init__(self) -> None:
        self.items: List[Finding] = []
        self._counters: Dict[str, int] = {}

    def add(self, finding: Finding) -> Finding:
        prefix = CATEGORIES.get(finding.category, "OTH")
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        finding.id = f"{prefix}-{self._counters[prefix]:04d}"
        self.items.append(finding)
        return finding

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def by_id(self) -> Dict[str, Finding]:
        return {f.id: f for f in self.items}

    def of(self, *categories: str) -> List[Finding]:
        return [f for f in self.items if f.category in categories]
