"""Regex/scanner member extraction (v1).

Used for languages without a tree-sitter grammar (Visual Basic)
and as a fallback when tree-sitter cannot parse a file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..inventory import FileInfo, Inventory
from ...utils.text import (SYNTAXES, LineMap, blank_c, block_safe, brace_pairs, classify_lines,
                           member_end, split_lines)

MODS = (r"public|protected|private|static|final|synchronized|abstract|native|strictfp|default|"
        r"transient|volatile|internal|virtual|override|async|sealed|extern|unsafe|partial|new|"
        r"readonly|const|event")
_TYPE = r"[\w$.<>\[\]?]+(?:,[ \t]*[\w$.<>\[\]?]+)*(?:[ \t]*\[[ \t]*\])*"
METHOD_RE = re.compile(r"(?m)^[ \t]*((?:(?:" + MODS + r")\s+)*)(" + _TYPE + r")[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(")
FIELD_RE = re.compile(r"(?m)^[ \t]*((?:(?:" + MODS + r")\s+)+)(" + _TYPE + r")[ \t]+([A-Za-z_$][\w$]*)[ \t]*(=|;)")
JS_FUNC_RE = re.compile(r"(?m)^[ \t]*(?:async[ \t]+)?function[ \t]*\*?[ \t]*([A-Za-z_$][\w$]*)[ \t]*\(")
PY_DEF_RE = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)[ \t]*\(")
VB_PROC_RE = re.compile(r"^[ \t]*(?:(Public|Private|Friend|Global)[ \t]+)?(?:Static[ \t]+)?"
                        r"(Sub|Function|Property[ \t]+(?:Get|Let|Set))[ \t]+([A-Za-z_]\w*)", re.I)
VB_END_RE = re.compile(r"^[ \t]*End[ \t]+(Sub|Function|Property)\b", re.I)
NOT_TYPES = {"return", "new", "throw", "else", "case", "if", "while", "for", "switch", "catch",
             "synchronized", "do", "try", "assert", "goto", "yield", "await", "package", "import",
             "extends", "implements", "throws", "instanceof", "in", "is", "as", "using", "lock",
             "foreach", "delete", "typeof", "sizeof", "default"}


# ---------------------------------------------------------------------------
# Member declarations
# ---------------------------------------------------------------------------

@dataclass
class Member:
    rel: str
    lang: str
    name: str
    kind: str                 # method | field | function | procedure
    private: bool
    start_line: int
    end_line: int
    decl_line: int = 0
    skip: Optional[str] = None
    in_type_with_supertype: bool = False


def _type_blocks(blanked: str, pairs: Dict[int, int]) -> List[Tuple[int, int, str]]:
    """(open, close, kind) for every brace block; kind: type | anon | interface | code."""
    blocks = []
    for open_pos, close_pos in pairs.items():
        k = open_pos - 1
        while k >= 0 and blanked[k] not in ";{}":
            k -= 1
        header = blanked[k + 1:open_pos]
        if re.search(r"\binterface\b", header):
            kind = "interface"
        elif re.search(r"\b(class|enum|record|struct)\b", header) and not re.search(r"\bnew\b", header):
            kind = "type:super" if re.search(r"\b(extends|implements)\b|:\s*[A-Z]", header) else "type"
        elif re.search(r"\bnew\b", header) and ")" in header:
            kind = "anon"
        else:
            kind = "code"
        blocks.append((open_pos, close_pos, kind))
    blocks.sort()
    return blocks


def _enclosing(blocks: List[Tuple[int, int, str]], pos: int) -> Optional[str]:
    best = None
    for open_pos, close_pos, kind in blocks:
        if open_pos >= pos:
            break
        if close_pos > pos:
            best = kind
    return best


def _comment_lines_above(cl, decl_line: int) -> int:
    """First line of the comment block directly above decl_line (or decl_line)."""
    start = decl_line
    while start > 1 and cl.lines[start - 2].kind == "comment":
        start -= 1
    if start < decl_line and not block_safe(cl, start, decl_line - 1):
        return decl_line
    return start


def _annotated(lines: List[str], decl_line: int) -> bool:
    k = decl_line - 2
    while k >= 0 and lines[k].strip():
        stripped = lines[k].strip()
        if stripped.startswith("@") or stripped.startswith("[") and stripped.endswith("]"):
            return True
        if not (stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*")):
            break
        k -= 1
    return False


def _tidy_end(lines: List[str], start: int, end: int) -> int:
    before = lines[start - 2].strip() if start >= 2 else ""
    after = lines[end].strip() if end < len(lines) else "x"
    return end + 1 if before == "" and after == "" and end < len(lines) else end


def extract_members_regex(inv: Inventory, fi: FileInfo) -> List[Member]:
    text = inv.text(fi)
    if not text:
        return []
    lines = split_lines(text)
    if fi.ext in ("java", "cs"):
        return _c_family_members(fi, text, lines, java_like=True)
    if fi.ext in ("js", "mjs", "cjs", "jsx"):
        return _c_family_members(fi, text, lines, java_like=False)
    if fi.ext == "py":
        return _python_members(fi, lines)
    if fi.ext in ("bas", "cls", "frm", "ctl", "dsr", "vb"):
        return _vb_members(fi, lines)
    return []


def _c_family_members(fi: FileInfo, text: str, lines: List[str], java_like: bool) -> List[Member]:
    blanked = blank_c(text)
    pairs, balanced = brace_pairs(blanked)
    if not balanced:
        return []
    lmap = LineMap(text)
    cl = classify_lines(lines, SYNTAXES["c"])
    blocks = _type_blocks(blanked, pairs)
    out: List[Member] = []

    def add(name: str, kind: str, mods: Set[str], decl_pos: int, name_pos: int, end_kind: str) -> None:
        end = member_end(blanked, name_pos, pairs, end_kind)
        if end is None:
            return
        decl_line = lmap.line(decl_pos)
        end_line = lmap.line(end - 1)
        tail = blanked[end:].split("\n", 1)[0]
        if tail.strip():
            return  # other code shares the closing line
        start_line = _comment_lines_above(cl, decl_line)
        member = Member(rel=fi.rel, lang="java" if java_like else "js", name=name, kind=kind,
                        private="private" in mods, start_line=start_line,
                        end_line=_tidy_end(lines, start_line, end_line), decl_line=decl_line)
        enclosing = _enclosing(blocks, decl_pos) if java_like else None
        if java_like:
            if enclosing not in ("type", "type:super", "interface"):
                return
            member.in_type_with_supertype = enclosing == "type:super"
            if enclosing == "interface" and not member.private:
                member.skip = "miembro de una interfaz"
        if mods & {"native", "abstract", "override", "extern", "partial"}:
            member.skip = "declaración sin cuerpo propio o sobrescrita"
        elif _annotated(lines, decl_line):
            member.skip = "tiene anotaciones o atributos"
        out.append(member)

    if java_like:
        for m in METHOD_RE.finditer(blanked):
            type_word = m.group(2).split()[-1] if m.group(2).split() else ""
            if type_word in NOT_TYPES or m.group(3) in NOT_TYPES:
                continue
            add(m.group(3), "method", set(m.group(1).split()), m.start(), m.start(3), "body")
        for m in FIELD_RE.finditer(blanked):
            if m.group(3) in NOT_TYPES:
                continue
            add(m.group(3), "field", set(m.group(1).split()), m.start(), m.start(3), "field")
    else:
        for m in JS_FUNC_RE.finditer(blanked):
            line_text = blanked[m.start():m.end()]
            if "export" in line_text:
                continue
            add(m.group(1), "function", set(), m.start(), m.start(1), "body")
    return out


def _python_members(fi: FileInfo, lines: List[str]) -> List[Member]:
    out: List[Member] = []
    cl = classify_lines(lines, SYNTAXES["hash"])
    for idx, raw in enumerate(lines):
        m = PY_DEF_RE.match(raw)
        if not m:
            continue
        indent = len(m.group(1).expandtabs(4))
        name = m.group(2)
        end = idx
        for j in range(idx + 1, len(lines)):
            stripped = lines[j].strip()
            if not stripped:
                continue
            line_indent = len(lines[j].expandtabs(4)) - len(lines[j].expandtabs(4).lstrip())
            if line_indent <= indent:
                if stripped.startswith("#"):
                    continue            # comment at the outer level: not part of the body
                if stripped.startswith((")", "]")):
                    end = j             # closing bracket of a multi-line signature
                    continue
                break
            end = j
        decl_line = idx + 1
        start_line = _comment_lines_above(cl, decl_line)
        member = Member(rel=fi.rel, lang="py", name=name, kind="function",
                        private=name.startswith("_") and not name.startswith("__"),
                        start_line=start_line, end_line=_tidy_end(lines, start_line, end + 1),
                        decl_line=decl_line)
        prev = lines[idx - 1].strip() if idx else ""
        if prev.startswith("@"):
            member.skip = "función decorada (registro por framework)"
        if name.startswith("__") and name.endswith("__"):
            member.skip = "método especial de Python"
        out.append(member)
    return out


def _vb_members(fi: FileInfo, lines: List[str]) -> List[Member]:
    out: List[Member] = []
    cl = classify_lines(lines, SYNTAXES["vb"])
    for idx, raw in enumerate(lines):
        m = VB_PROC_RE.match(raw)
        if not m:
            continue
        end = None
        for j in range(idx + 1, len(lines)):
            if VB_END_RE.match(lines[j]):
                end = j
                break
            if VB_PROC_RE.match(lines[j]):
                break
        if end is None:
            continue
        name = m.group(3)
        decl_line = idx + 1
        start_line = _comment_lines_above(cl, decl_line)
        member = Member(rel=fi.rel, lang="vb", name=name, kind="procedure",
                        private=(m.group(1) or "").lower() == "private",
                        start_line=start_line, end_line=_tidy_end(lines, start_line, end + 1),
                        decl_line=decl_line)
        if "_" in name and fi.ext in ("frm", "ctl", "cls", "dsr"):
            member.skip = "manejador de evento enlazado por nombre"
        if name.lower() in ("main", "class_initialize", "class_terminate"):
            member.skip = "punto de entrada o ciclo de vida"
        out.append(member)
    return out


