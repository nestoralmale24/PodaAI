"""Read-only tools for the LLM agent, sandboxed to the project.

Files are read through the inventory, so the agent sees exactly what Poda AI
analyses: legacy encodings are decoded, VCS metadata, dependency caches and the
previously generated documentation are invisible, and nothing can be written.
"""
from __future__ import annotations

import fnmatch
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .inventory import FileInfo, Inventory
from ..utils.text import split_lines, strip_eol

MAX_OUTPUT = 6000
MAX_READ_LINES = 400

TOOL_SPECS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search the contents of the project's text files with a regular expression "
                       "(case-insensitive unless case_sensitive is true). Returns `path:line: text` lines. "
                       "Lines from documentation, generated files or files already flagged for removal "
                       "are tagged.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regular expression (Python syntax)."},
            "path": {"type": "string", "description": "File or directory relative to the project root. Default: whole project."},
            "glob": {"type": "string", "description": "Only files whose relative path matches this glob, e.g. **/*.xml"},
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer", "description": "Default 40, maximum 200."}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": f"Read a range of lines of a text file (at most {MAX_READ_LINES} lines per call). "
                       "Lines are numbered.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File relative to the project root."},
            "start_line": {"type": "integer", "description": "1-based, default 1."},
            "end_line": {"type": "integer", "description": "Inclusive."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "glob",
        "description": "List project files whose relative path matches a glob pattern, e.g. **/*.jsp",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List the files and subdirectories of a directory of the project.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory relative to the project root. Default: root."}}}}},
]


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[... salida recortada a {limit} caracteres]"


class ReadOnlyTools:
    def __init__(self, inv: Inventory) -> None:
        self.inv = inv
        self.flagged: Set[str] = set()   # files proposed for removal (tagged in results)

    # -- helpers ------------------------------------------------------------------
    @staticmethod
    def specs() -> List[Dict[str, Any]]:
        return TOOL_SPECS

    @staticmethod
    def _norm(path: Optional[str]) -> Optional[str]:
        if path is None:
            return None
        path = str(path).replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        path = path.strip("/")
        if path in ("", "."):
            return ""
        if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or ".." in path.split("/"):
            raise ValueError("ruta fuera del proyecto")
        return path

    def tag(self, fi: FileInfo) -> str:
        if fi.rel in self.flagged:
            return " [propuesto para eliminar]"
        if fi.generated:
            return " [generado]"
        if fi.kind == "doc":
            return " [documentación]"
        return ""

    def files(self, path: Optional[str] = None, glob: Optional[str] = None) -> Iterable[FileInfo]:
        prefix = self._norm(path) or ""
        for fi in self.inv.files:
            if prefix and not (fi.rel == prefix or fi.rel.startswith(prefix + "/")):
                continue
            if glob and not (fnmatch.fnmatch(fi.rel, glob) or fnmatch.fnmatch(fi.name, glob)):
                continue
            yield fi

    def search(self, regex: "re.Pattern[str]", path: Optional[str] = None, glob: Optional[str] = None,
               limit: int = 40, exclude: Optional[str] = None) -> Tuple[List[Tuple[FileInfo, int, str]], int]:
        hits: List[Tuple[FileInfo, int, str]] = []
        total = 0
        for fi in self.files(path, glob):
            if fi.rel == exclude or not self.inv.is_text(fi):
                continue
            text = self.inv.text(fi)
            if not text or not regex.search(text):
                continue
            for number, line in enumerate(split_lines(text), start=1):
                if regex.search(line):
                    total += 1
                    if len(hits) < limit:
                        hits.append((fi, number, strip_eol(line).strip()[:220]))
        return hits, total

    # -- tools ------------------------------------------------------------------------
    def run(self, name: str, args: Dict[str, Any]) -> str:
        try:
            if name == "grep":
                return self.grep(**args)
            if name == "read_file":
                return self.read_file(**args)
            if name == "glob":
                return self.glob(**args)
            if name == "list_dir":
                return self.list_dir(**args)
            return f"Error: herramienta desconocida '{name}'. Disponibles: grep, read_file, glob, list_dir."
        except TypeError as exc:
            return f"Error: argumentos no válidos para {name}: {exc}"
        except ValueError as exc:
            return f"Error: {exc}"

    def grep(self, pattern: str, path: Optional[str] = None, glob: Optional[str] = None,
             case_sensitive: bool = False, max_results: int = 40, **_: Any) -> str:
        flags = 0 if case_sensitive else re.I
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags)
        limit = max(1, min(int(max_results or 40), 200))
        hits, total = self.search(regex, path, glob, limit)
        if not hits:
            return f'Sin coincidencias para "{pattern}"' + (f" en {path}" if path else "")
        lines = [f"{fi.rel}:{number}: {text}{self.tag(fi)}" for fi, number, text in hits]
        head = f"{total} coincidencias" + (f" (se muestran {len(hits)})" if total > len(hits) else "")
        return _clip(head + "\n" + "\n".join(lines))

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None, **_: Any) -> str:
        rel = self._norm(path)
        fi = self.inv.by_rel.get(rel or "")
        if fi is None:
            return f"Error: no existe el fichero {path}"
        if not self.inv.is_text(fi):
            return f"{rel}: fichero binario ({fi.size} bytes), no se puede leer como texto"
        lines = split_lines(self.inv.text(fi) or "")
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:6d}  {strip_eol(lines[n - 1])}" for n in range(start, end + 1))
        head = f"{rel} (líneas {start}-{end} de {len(lines)}){self.tag(fi)}"
        return _clip(head + "\n" + body)

    def glob(self, pattern: str, **_: Any) -> str:
        found = [fi.rel + self.tag(fi) for fi in self.files(glob=pattern)]
        if not found:
            return f"Ningún fichero coincide con {pattern}"
        shown = found[:200]
        return _clip(f"{len(found)} ficheros" + (" (se muestran 200)" if len(found) > 200 else "") + "\n"
                     + "\n".join(shown))

    def list_dir(self, path: Optional[str] = None, **_: Any) -> str:
        base = self._norm(path) or ""
        prefix = base + "/" if base else ""
        dirs = sorted({d[len(prefix):].split("/", 1)[0] + "/" for d in self.inv.dirs if d.startswith(prefix)})
        files = sorted(fi.name + self.tag(fi) for fi in self.inv.files if fi.parent == base)
        if not dirs and not files:
            return f"Error: el directorio {path or '.'} no existe o está vacío"
        entries = dirs + files
        return _clip(f"{base or '.'}: {len(dirs)} directorios, {len(files)} ficheros\n" + "\n".join(entries[:300]))

    @staticmethod
    def describe(name: str, args: Dict[str, Any]) -> str:
        if name == "grep":
            text = f'🔎 busca "{args.get("pattern")}"'
            if args.get("path"):
                text += f" en {args['path']}"
            return text + (f" [{args['glob']}]" if args.get("glob") else "")
        if name == "read_file":
            text = f"📖 lee {args.get('path')}"
            if args.get("start_line") or args.get("end_line"):
                text += f" (líneas {args.get('start_line') or 1}-{args.get('end_line') or '…'})"
            return text
        if name == "glob":
            return f"🗂  lista {args.get('pattern')}"
        if name == "list_dir":
            return f"📂 abre {args.get('path') or '.'}"
        return f"🛠  {name} {json.dumps(args, ensure_ascii=False)[:100]}"
