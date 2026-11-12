"""Checks that an edited file is still well-formed; if not, the edit is reverted.

A file is only judged against itself: if it was already broken before the edit,
the edit is accepted as long as it does not make it worse.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Tuple

from ..utils.text import braces_balanced, decode
from .analyzers import ast_members

BRACE_EXT = {"java", "cs", "js", "mjs", "cjs", "jsx", "ts", "tsx", "c", "h", "cpp", "hpp", "cc", "cxx",
             "php", "kt", "scala", "groovy", "go", "swift", "css", "scss", "less"}
XML_EXT = {"xml", "xsd", "tld", "wsdl", "xsl", "xslt", "xhtml", "jspx", "tagx", "csproj", "vbproj",
           "config", "hbm", "jrxml"}


def _xml_ok(data: bytes) -> bool:
    try:
        ET.fromstring(data)
        return True
    except ET.ParseError:
        return False


def _json_ok(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def _py_ok(text: str, name: str) -> bool:
    try:
        compile(text, name, "exec")
        return True
    except (SyntaxError, ValueError):
        return False


def _syntax_errors(ext: str, text: str) -> int:
    tree = ast_members.parse(ext, text.encode("utf-8"))
    if tree is None:
        return -1
    if not tree.root_node.has_error:
        return 0
    count, stack = 0, [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            count += 1
        stack.extend(node.children)
    return count


def validate(rel: str, before: bytes, after: bytes) -> Tuple[bool, str]:
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    text_before, _ = decode(before)
    text_after, _ = decode(after)
    if ext in BRACE_EXT and braces_balanced(text_before) and not braces_balanced(text_after):
        return False, "las llaves quedaban desequilibradas"
    if ext in XML_EXT and _xml_ok(before) and not _xml_ok(after):
        return False, "el XML dejaba de estar bien formado"
    if ext == "json" and _json_ok(text_before) and not _json_ok(text_after):
        return False, "el JSON dejaba de ser válido"
    if ext == "py" and _py_ok(text_before, rel) and not _py_ok(text_after, rel):
        return False, "el Python dejaba de compilar"
    if ext in ast_members.GRAMMARS:
        errors_before = _syntax_errors(ext, text_before)
        if errors_before >= 0 and _syntax_errors(ext, text_after) > errors_before:
            return False, "el análisis sintáctico (tree-sitter) encontraba errores nuevos"
    return True, ""
