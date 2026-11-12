"""tree-sitter analysis: exact member extents and provably unreachable code.

* ``extract`` returns the members of a Java, C#, Python, JavaScript or
  TypeScript file with the exact span of their syntax node (annotations and the
  documentation comment directly above included). It returns None when the
  language has no grammar or the file does not parse cleanly, so the caller can
  fall back to the v1 scanners.
* ``detect_unreachable`` finds code that can never run, provable from the file
  alone: branches under a literal or ``final``/``const`` false condition, and
  statements after ``return``/``throw``/``raise`` in the same block.
"""
from __future__ import annotations

import importlib
import re
from functools import lru_cache
from typing import Dict, Iterator, List, Optional, Set, Tuple

from ..inventory import FileInfo, Inventory, is_vendor
from ...models import Finding, Findings
from ...utils.text import strip_eol
from .legacy_scanners import Member

GRAMMARS: Dict[str, Tuple[str, str]] = {
    "java": ("tree_sitter_java", "language"),
    "cs": ("tree_sitter_c_sharp", "language"),
    "py": ("tree_sitter_python", "language"),
    "js": ("tree_sitter_javascript", "language"),
    "mjs": ("tree_sitter_javascript", "language"),
    "cjs": ("tree_sitter_javascript", "language"),
    "jsx": ("tree_sitter_javascript", "language"),
    "ts": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
}
LANG_OF = {"java": "java", "cs": "cs", "py": "py", "js": "js", "mjs": "js", "cjs": "js", "jsx": "js",
           "ts": "js", "tsx": "js"}
COMMENT_TYPES = {"comment", "line_comment", "block_comment"}
TERMINATORS = {"return_statement", "throw_statement", "raise_statement", "break_statement",
               "continue_statement"}
HOISTED = {"function_declaration", "generator_function_declaration", "class_declaration",
           "local_function_statement", "function_definition", "class_definition"}


@lru_cache(maxsize=None)
def _language(ext: str):
    spec = GRAMMARS.get(ext)
    if not spec:
        return None
    try:
        from tree_sitter import Language
        module = importlib.import_module(spec[0])
        return Language(getattr(module, spec[1])())
    except Exception:
        return None


def parse(ext: str, source: bytes):
    """Parse source bytes with the grammar for `ext`; None if unavailable."""
    language = _language(ext)
    if language is None:
        return None
    from tree_sitter import Parser
    return Parser(language).parse(source)


def available(ext: str) -> bool:
    return _language(ext) is not None


class _Source:
    """UTF-8 view of a decoded file. tree-sitter rows match our line numbers."""

    def __init__(self, text: str) -> None:
        self.data = text.encode("utf-8")
        self.lines = self.data.split(b"\n")

    def only_node_on_lines(self, node) -> bool:
        start_row, start_col = node.start_point
        end_row, end_col = node.end_point
        before = self.lines[start_row][:start_col]
        after = self.lines[end_row][end_col:] if end_row < len(self.lines) else b""
        return not before.strip() and not after.strip()

    def blank(self, row: int) -> bool:
        return 0 <= row < len(self.lines) and not self.lines[row].strip()


def _usable(text: str) -> bool:
    # A lone CR (classic Mac line endings) would shift tree-sitter rows.
    return not re.search(r"\r(?!\n)", text)


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text is not None else ""


def _walk(node) -> Iterator:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _span(src: _Source, node) -> Optional[Tuple[int, int]]:
    """1-based line span of a node plus the comments directly above it."""
    if not src.only_node_on_lines(node):
        return None
    start = node.start_point[0]
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in COMMENT_TYPES \
            and sibling.end_point[0] == start - 1 and src.only_node_on_lines(sibling):
        start = sibling.start_point[0]
        sibling = sibling.prev_sibling
    end = node.end_point[0]
    if src.blank(start - 1) and src.blank(end + 1):
        end += 1
    return start + 1, end + 1


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def extract(inv: Inventory, fi: FileInfo) -> Optional[List[Member]]:
    lang = LANG_OF.get(fi.ext)
    if lang is None or not available(fi.ext):
        return None
    text = inv.text(fi)
    if text is None:
        return []
    if not _usable(text):
        return None
    src = _Source(text)
    tree = parse(fi.ext, src.data)
    if tree is None or tree.root_node.has_error:
        return None
    if lang == "java":
        return list(_java_members(fi, src, tree.root_node))
    if lang == "cs":
        return list(_cs_members(fi, src, tree.root_node))
    if lang == "py":
        return list(_python_members(fi, src, tree.root_node))
    return list(_js_members(fi, src, tree.root_node))


def _member(fi: FileInfo, lang: str, name_node, node, kind: str, private: bool, src: _Source,
            skip: Optional[str] = None, supertype: bool = False) -> Optional[Member]:
    if name_node is None:
        return None
    span = _span(src, node)
    if span is None:
        return None
    return Member(rel=fi.rel, lang=lang, name=_text(name_node), kind=kind, private=private,
                  start_line=span[0], end_line=span[1], decl_line=name_node.start_point[0] + 1,
                  skip=skip, in_type_with_supertype=supertype)


JAVA_TYPES = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}


def _java_members(fi: FileInfo, src: _Source, root) -> Iterator[Member]:
    for type_node in _walk(root):
        if type_node.type not in JAVA_TYPES:
            continue
        body = type_node.child_by_field_name("body")
        if body is None:
            continue
        is_interface = type_node.type == "interface_declaration"
        supertype = any(type_node.child_by_field_name(f) is not None
                        for f in ("superclass", "interfaces")) \
            or any(c.type in ("superclass", "super_interfaces") for c in type_node.children)
        members = list(body.named_children)
        for child in list(members):
            if child.type == "enum_body_declarations":
                members.extend(child.named_children)
        for node in members:
            if node.type not in ("method_declaration", "field_declaration", "constant_declaration"):
                continue
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            mods = {c.type for c in modifiers.children} if modifiers is not None else set()
            annotated = bool(mods & {"marker_annotation", "annotation"})
            private = "private" in mods
            skip = None
            if annotated:
                skip = "tiene anotaciones"
            elif is_interface and not private:
                skip = "miembro de una interfaz"
            elif mods & {"native", "abstract"}:
                skip = "declaración sin cuerpo propio"
            if node.type == "method_declaration":
                if node.child_by_field_name("body") is None and skip is None:
                    skip = "declaración sin cuerpo propio"
                found = _member(fi, "java", node.child_by_field_name("name"), node, "method", private, src,
                                skip, supertype)
            else:
                declarators = [c for c in node.named_children if c.type == "variable_declarator"]
                if len(declarators) != 1:
                    continue
                found = _member(fi, "java", declarators[0].child_by_field_name("name"), node, "field",
                                private, src, skip, supertype)
            if found:
                yield found


CS_TYPES = {"class_declaration", "struct_declaration", "interface_declaration", "record_declaration"}


def _cs_modifiers(node) -> Set[str]:
    return {_text(c) for c in node.children if c.type == "modifier"}


def _cs_members(fi: FileInfo, src: _Source, root) -> Iterator[Member]:
    for type_node in _walk(root):
        if type_node.type not in CS_TYPES:
            continue
        body = type_node.child_by_field_name("body") or next(
            (c for c in type_node.children if c.type == "declaration_list"), None)
        if body is None:
            continue
        type_mods = _cs_modifiers(type_node)
        is_interface = type_node.type == "interface_declaration"
        supertype = any(c.type == "base_list" for c in type_node.children)
        for node in body.named_children:
            if node.type not in ("method_declaration", "field_declaration"):
                continue
            mods = _cs_modifiers(node)
            private = ("private" in mods or not mods & {"public", "protected", "internal"}) \
                and "partial" not in type_mods
            skip = None
            if any(c.type == "attribute_list" for c in node.children):
                skip = "tiene atributos"
            elif is_interface and not private:
                skip = "miembro de una interfaz"
            elif mods & {"override", "virtual", "abstract", "extern", "partial"}:
                skip = "sobrescritura o declaración externa"
            if node.type == "method_declaration":
                found = _member(fi, "cs", node.child_by_field_name("name"), node, "method", private, src,
                                skip, supertype)
            else:
                declaration = next((c for c in node.named_children if c.type == "variable_declaration"), None)
                declarators = [c for c in declaration.named_children if c.type == "variable_declarator"] \
                    if declaration is not None else []
                if len(declarators) != 1:
                    continue
                name = declarators[0].child_by_field_name("name") or next(
                    (c for c in declarators[0].named_children if c.type == "identifier"), None)
                found = _member(fi, "cs", name, node, "field", private, src, skip, supertype)
            if found:
                yield found


def _python_members(fi: FileInfo, src: _Source, root) -> Iterator[Member]:
    for node in _walk(root):
        if node.type != "function_definition":
            continue
        name_node = node.child_by_field_name("name")
        name = _text(name_node)
        skip = None
        if node.parent is not None and node.parent.type == "decorated_definition":
            skip = "función decorada (registro por framework)"
        elif name.startswith("__") and name.endswith("__"):
            skip = "método especial de Python"
        private = name.startswith("_") and not name.startswith("__")
        found = _member(fi, "py", name_node, node, "function", private, src, skip)
        if found:
            yield found


def _js_members(fi: FileInfo, src: _Source, root) -> Iterator[Member]:
    for node in _walk(root):
        if node.type in ("function_declaration", "generator_function_declaration"):
            if node.parent is not None and node.parent.type == "export_statement":
                continue
            found = _member(fi, "js", node.child_by_field_name("name"), node, "function", False, src)
        elif node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            accessibility = next((c for c in node.children if c.type == "accessibility_modifier"), None)
            private = (name_node is not None and name_node.type == "private_property_identifier") \
                or _text(accessibility) == "private"
            if not private:
                continue
            found = _member(fi, "js", name_node, node, "method", True, src)
            if found and found.name.startswith("#"):
                found.name = found.name[1:]
        else:
            continue
        if found:
            yield found


# ---------------------------------------------------------------------------
# Unreachable code
# ---------------------------------------------------------------------------

FALSE_LITERALS = {"false", "False", "0"}


def _false_constants(lang: str, root) -> Set[str]:
    """Names of constants that are false for the whole file (static final / const)."""
    names: Set[str] = set()
    for node in _walk(root):
        if lang == "java" and node.type == "field_declaration":
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            mods = {c.type for c in modifiers.children} if modifiers is not None else set()
            if {"static", "final"} <= mods:
                for decl in (c for c in node.named_children if c.type == "variable_declarator"):
                    value = decl.child_by_field_name("value")
                    if value is not None and _text(value) == "false":
                        names.add(_text(decl.child_by_field_name("name")))
        elif lang == "cs" and node.type == "field_declaration" and "const" in _cs_modifiers(node):
            for decl in _walk(node):
                if decl.type == "variable_declarator":
                    parts = [_text(c) for c in decl.named_children]
                    if len(parts) >= 2 and parts[-1] == "false":
                        names.add(parts[0])
        elif lang == "js" and node.type == "lexical_declaration" and node.parent is not None \
                and node.parent.type == "program" and _text(node).startswith("const"):
            for decl in (c for c in node.named_children if c.type == "variable_declarator"):
                value = decl.child_by_field_name("value")
                if value is not None and _text(value) == "false":
                    names.add(_text(decl.child_by_field_name("name")))
    return names


def _condition_text(node) -> str:
    condition = node.child_by_field_name("condition")
    text = _text(condition).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def detect_unreachable(inv: Inventory, findings: Findings) -> int:
    count = 0
    for fi in inv.files:
        lang = LANG_OF.get(fi.ext)
        if lang is None or fi.excluded or fi.generated or fi.rel in inv.junk or is_vendor(fi.rel):
            continue
        if not available(fi.ext):
            continue
        text = inv.text(fi)
        if not text or not _usable(text):
            continue
        src = _Source(text)
        tree = parse(fi.ext, src.data)
        if tree is None or tree.root_node.has_error:
            continue
        constants = _false_constants(lang, tree.root_node)
        lines = text.split("\n")
        for node in _walk(tree.root_node):
            if node.type == "if_statement":
                condition = _condition_text(node)
                if condition not in FALSE_LITERALS and condition not in constants:
                    continue
                if node.parent is not None and node.parent.type in ("else_clause", "else"):
                    continue  # `else if (false)`: part of a chain, leave it to a human
                alternative = node.child_by_field_name("alternative")
                start, end = node.start_point[0] + 1, node.end_point[0] + 1
                snippet = "\n".join(strip_eol(x) for x in lines[start - 1:min(end, start + 7)])
                if alternative is not None or not src.only_node_on_lines(node):
                    findings.add(Finding(
                        category="unreachable_code", subtype="constant_false_branch", path=fi.rel,
                        start_line=start, end_line=end, symbol=condition,
                        reason=f"rama que nunca se ejecuta (condición siempre falsa: {condition}); "
                               "tiene else o comparte línea: simplificar a mano",
                        confidence="high", needs_llm=False, detectors=["ast"], snippet=snippet))
                    continue
                findings.add(Finding(
                    category="unreachable_code", subtype="constant_false_branch", path=fi.rel,
                    start_line=start, end_line=end, symbol=condition,
                    reason=f"rama que nunca se ejecuta (condición siempre falsa: {condition})",
                    confidence="high", needs_llm=False, detectors=["ast"],
                    evidence=[f"La condición `{condition}` es constante y falsa en todo el fichero"],
                    snippet=snippet, edit={"op": "delete_lines", "ranges": [[start, end]]}))
                count += 1
            elif node.type in ("block", "statement_block"):
                count += _after_terminator(fi, src, node, lines, findings)
    return count


def _after_terminator(fi: FileInfo, src: _Source, block, lines: List[str], findings: Findings) -> int:
    statements = [c for c in block.named_children if c.type not in COMMENT_TYPES]
    for index, statement in enumerate(statements):
        if statement.type not in TERMINATORS:
            continue
        dead = [s for s in statements[index + 1:] if s.type not in HOISTED
                and not (s.type == "variable_declaration" and _text(s).startswith("var"))]
        if not dead or len(dead) != len(statements) - index - 1:
            return 0
        first, last = dead[0], dead[-1]
        if not (src.only_node_on_lines(first) and src.only_node_on_lines(last)):
            return 0
        start, end = first.start_point[0] + 1, last.end_point[0] + 1
        if end >= block.end_point[0] + 1 and block.type != "block":
            return 0  # the closing brace shares the last line
        snippet = "\n".join(strip_eol(x) for x in lines[start - 1:min(end, start + 7)])
        findings.add(Finding(
            category="unreachable_code", subtype="after_terminator", path=fi.rel,
            start_line=start, end_line=end, symbol=_text(statement).split("\n")[0][:60],
            reason=f"código tras `{_text(statement).split()[0]}` en el mismo bloque: nunca se ejecuta",
            confidence="high", needs_llm=False, detectors=["ast"], snippet=snippet,
            edit={"op": "delete_lines", "ranges": [[start, end]]}))
        return 1
    return 0
