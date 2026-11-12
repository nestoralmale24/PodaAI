"""Byte-exact line handling, encoding detection and comment/string-aware scanners.

Poda AI never re-encodes a legacy file: edits are applied on raw bytes, line by
line. Lines are split only on CRLF, LF and CR, because str.splitlines() also
splits on characters such as U+0085 or form feeds, which would shift line
numbers in cp1252/latin-1 files.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_LINES_B = re.compile(rb"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+\Z")
_LINES_S = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+\Z")
_EOL_S = re.compile(r"\r\n|\n|\r")


def split_lines_b(data: bytes) -> List[bytes]:
    """Split bytes into lines, keeping each line ending."""
    return _LINES_B.findall(data)


def split_lines(text: str) -> List[str]:
    """Split text into lines, keeping each line ending (CRLF, LF, CR only)."""
    return _LINES_S.findall(text)


def strip_eol(line: str) -> str:
    return line.rstrip("\r\n")


def decode(data: bytes) -> Tuple[str, str]:
    """Decode legacy text: UTF-8 (with or without BOM), then cp1252, then latin-1."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def is_binary(head: bytes) -> bool:
    return b"\x00" in head


def norm_ws(text: str) -> str:
    """Normalise whitespace for tolerant line comparisons."""
    return " ".join(text.replace("﻿", "").split())


class LineMap:
    """Maps character offsets of a text to 1-based line numbers."""

    def __init__(self, text: str) -> None:
        self.starts = [0] + [m.end() for m in _EOL_S.finditer(text)]

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)


# ---------------------------------------------------------------------------
# Comment syntaxes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Syntax:
    name: str
    line: Tuple[str, ...] = ()
    block: Tuple[Tuple[str, str], ...] = ()
    strings: Tuple[str, ...] = ()
    escape: Optional[str] = "\\"
    doubled_quote: bool = False   # '' or "" escapes a quote (VB, SQL, Pascal)
    rem: bool = False             # REM statements (VB, batch)
    cobol: bool = False


SYNTAXES: Dict[str, Syntax] = {
    "c": Syntax("c", line=("//",), block=(("/*", "*/"),), strings=('"', "'", "`")),
    "css": Syntax("css", block=(("/*", "*/"),), strings=('"', "'")),
    "xml": Syntax("xml", block=(("<!--", "-->"),)),
    "jsp": Syntax("jsp", line=("//",), block=(("<%--", "--%>"), ("<!--", "-->"), ("/*", "*/")),
                  strings=('"',)),
    "vb": Syntax("vb", line=("'",), strings=('"',), escape=None, doubled_quote=True, rem=True),
    "sql": Syntax("sql", line=("--",), block=(("/*", "*/"),), strings=("'",), escape=None,
                  doubled_quote=True),
    "hash": Syntax("hash", line=("#",), strings=('"', "'")),
    "pascal": Syntax("pascal", line=("//",), block=(("{", "}"), ("(*", "*)")), strings=("'",),
                     escape=None, doubled_quote=True),
    "bat": Syntax("bat", line=("::",), strings=('"',), escape=None, rem=True),
    "ini": Syntax("ini", line=(";", "#")),
    "cobol": Syntax("cobol", cobol=True),
}

EXT_SYNTAX: Dict[str, str] = {}
for _syntax, _exts in {
    "c": "java js mjs cjs jsx ts tsx c h cpp hpp cc cxx hh cs php php3 php4 php5 phtml go kt kts "
         "scala groovy gradle swift m as scss less",
    "css": "css",
    "xml": "xml xsd xsl xslt wsdl tld html htm xhtml shtml vm ftl svg dtd xul config csproj vbproj "
           "resx aspx ascx master xaml jrxml hbm",
    "jsp": "jsp jspf jspx tag tagx",
    "vb": "bas cls frm ctl dsr vb vbs asp",
    "sql": "sql ddl pls plsql pkb pks prc fnc trg tps tpb",
    "hash": "py sh bash ksh zsh csh rb pl pm properties yml yaml conf r ps1 toml cfg mk",
    "pascal": "pas dpr dpk pp lpr",
    "bat": "bat cmd",
    "ini": "ini",
    "cobol": "cbl cob cpy pco ccp cobol",
}.items():
    for _ext in _exts.split():
        EXT_SYNTAX[_ext] = _syntax


@dataclass
class LineInfo:
    kind: str = "blank"            # blank | code | comment | mixed
    text: str = ""                 # comment text found on the line (markers stripped)
    opens: Optional[int] = None    # block comment that starts here and continues
    closes: Optional[int] = None   # block comment opened earlier that ends here
    inside: Optional[int] = None   # block comment the line starts inside
    doc: bool = False              # documentation comment (/** ... */, ///)
    marker: str = ""               # first comment opener seen on the line


@dataclass
class Classified:
    lines: List[LineInfo]
    open_line: Dict[int, int] = field(default_factory=dict)    # block id -> line
    close_line: Dict[int, int] = field(default_factory=dict)   # block id -> line


def _is_rem(line: str, j: int, has_code: bool) -> bool:
    if has_code or line[j:j + 3].lower() != "rem":
        return False
    return j + 3 == len(line) or line[j + 3] in " \t"


def classify_lines(lines: List[str], syn: Syntax) -> Classified:
    """Classify every line as blank, code, comment or mixed (code + comment)."""
    result = Classified(lines=[])
    block: Optional[Tuple[str, int, bool]] = None   # (closer, id, is_doc)
    next_id = 0
    for number, raw in enumerate(lines, start=1):
        line = strip_eol(raw)
        info = LineInfo()
        if syn.cobol:
            stripped = line.strip()
            if len(line) > 6 and line[6] in "*/":
                info.kind, info.text, info.marker = "comment", line[7:72], "*"
            elif stripped.startswith("*>"):
                info.kind, info.text, info.marker = "comment", stripped[2:], "*>"
            elif not line[6:].strip():
                info.kind = "blank"
            else:
                info.kind = "mixed" if "*>" in line[7:] else "code"
            result.lines.append(info)
            continue

        if block:
            info.inside = block[1]
            info.doc = block[2]
        j, n = 0, len(line)
        has_code = False
        parts: List[str] = []
        in_str: Optional[str] = None
        while j < n:
            if block:
                closer, bid, _doc = block
                k = line.find(closer, j)
                if k < 0:
                    parts.append(line[j:])
                    j = n
                    break
                parts.append(line[j:k])
                j = k + len(closer)
                if bid == info.inside:
                    info.closes = bid
                    result.close_line[bid] = number
                else:
                    info.opens = None
                    result.open_line.pop(bid, None)
                block = None
                continue
            ch = line[j]
            if in_str:
                if syn.escape and ch == syn.escape:
                    j += 2
                    continue
                if ch == in_str:
                    if syn.doubled_quote and j + 1 < n and line[j + 1] == in_str:
                        j += 2
                        continue
                    in_str = None
                j += 1
                continue
            opened = False
            for opener, closer in syn.block:
                if line.startswith(opener, j):
                    if syn.name == "pascal" and opener == "{" and line.startswith("{$", j):
                        break
                    is_doc = opener == "/*" and line.startswith("/**", j) and not line.startswith("/**/", j)
                    next_id += 1
                    block = (closer, next_id, is_doc)
                    info.opens = next_id
                    result.open_line[next_id] = number
                    info.doc = info.doc or is_doc
                    info.marker = info.marker or ("/**" if is_doc else opener)
                    j += len(opener)
                    opened = True
                    break
            if opened:
                continue
            prefix = next((p for p in syn.line if line.startswith(p, j)), None)
            if prefix is None and syn.rem and _is_rem(line, j, has_code):
                prefix = line[j:j + 3]
            if prefix is not None:
                if prefix == "//" and line.startswith("///", j) and syn.name == "c":
                    info.doc = True
                info.marker = info.marker or prefix
                parts.append(line[j + len(prefix):])
                j = n
                break
            if ch in syn.strings:
                in_str = ch
                has_code = True
                j += 1
                continue
            if not ch.isspace():
                has_code = True
            j += 1
        involved = bool(parts or info.inside or info.opens or info.closes or info.marker)
        if has_code and involved:
            info.kind = "mixed"
        elif has_code:
            info.kind = "code"
        elif involved:
            info.kind = "comment"
        else:
            info.kind = "blank"
        info.text = "".join(parts)
        result.lines.append(info)
    return result


def block_safe(cl: Classified, start: int, end: int) -> bool:
    """True if removing lines start..end (1-based) never splits a block comment."""
    for number in range(start, end + 1):
        info = cl.lines[number - 1]
        if info.opens is not None:
            close = cl.close_line.get(info.opens)
            if close is None or close > end:
                return False
        if info.closes is not None:
            opened = cl.open_line.get(info.closes)
            if opened is None or opened < start:
                return False
    return True


# ---------------------------------------------------------------------------
# C-family scanner (Java, C#, JavaScript, C/C++, PHP...)
# ---------------------------------------------------------------------------

_C_SPECIAL = re.compile(r"//|/\*|\"\"\"|[\"'`]")


def blank_c(text: str) -> str:
    """Return the text with comments and string contents replaced by spaces.

    Offsets and line breaks are preserved, so positions found in the result map
    1:1 to the original text.
    """
    out = list(text)
    n = len(text)

    def wipe(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] not in "\r\n":
                out[k] = " "

    i = 0
    while True:
        m = _C_SPECIAL.search(text, i)
        if not m:
            break
        tok, s = m.group(), m.start()
        if tok == "//":
            eol = _EOL_S.search(text, s)
            e = eol.start() if eol else n
            wipe(s, e)
            i = e
        elif tok == "/*":
            e = text.find("*/", s + 2)
            e = n if e < 0 else e + 2
            wipe(s, e)
            i = e
        elif tok == '"""':
            e = text.find('"""', s + 3)
            e = n if e < 0 else e + 3
            wipe(s + 3, max(s + 3, e - 3))
            i = e
        else:
            j = s + 1
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == tok:
                    break
                if ch in "\r\n" and tok != "`":
                    break
                j += 1
            wipe(s + 1, j)
            i = j + 1
    return "".join(out)


def brace_pairs(blanked: str) -> Tuple[Dict[int, int], bool]:
    """Map every '{' offset to its matching '}'. Second value: braces balanced."""
    stack: List[int] = []
    pairs: Dict[int, int] = {}
    balanced = True
    for m in re.finditer(r"[{}]", blanked):
        if m.group() == "{":
            stack.append(m.start())
        elif stack:
            pairs[stack.pop()] = m.start()
        else:
            balanced = False
    return pairs, balanced and not stack


def member_end(blanked: str, start: int, pairs: Dict[int, int], kind: str) -> Optional[int]:
    """Offset just after the end of a declaration that starts at `start`.

    kind "body" (methods, classes): ends at the brace closing the first
    top-level '{', or at a top-level ';' (abstract/interface methods).
    kind "field": ends at the first top-level ';' (initialisers may contain
    braces, e.g. array initialisers or anonymous classes).
    """
    depth = 0
    i, n = start, len(blanked)
    while i < n:
        ch = blanked[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "{":
            close = pairs.get(i)
            if close is None:
                return None
            if kind == "field" or depth > 0:
                i = close + 1
                continue
            return close + 1
        elif ch == "}":
            return None
        elif ch == ";" and depth <= 0:
            return i + 1
        i += 1
    return None


def braces_balanced(text: str) -> bool:
    return brace_pairs(blank_c(text))[1]
