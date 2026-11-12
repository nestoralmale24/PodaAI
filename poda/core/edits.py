"""Applies the decided removals to a copy of the project.

The original project is never modified. Edits are byte-exact (encodings and
line endings are preserved) and every modified file is validated: if a file
stops being well-formed (unbalanced braces, invalid XML/JSON, Python that no
longer compiles) the file is reverted and its findings are marked as failed.
"""
from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .inventory import Inventory
from ..models import Finding
from ..events import null_emit
from ..utils.text import decode, norm_ws, split_lines, split_lines_b
from .validators import validate

@dataclass
class ApplyStats:
    files_deleted: int = 0
    dirs_deleted: int = 0
    files_modified: int = 0
    lines_removed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    reverted: List[str] = field(default_factory=list)


def _strip_comma(line: bytes) -> bytes:
    body = line.rstrip(b"\r\n")
    eol = line[len(body):]
    stripped = body.rstrip()
    if stripped.endswith(b","):
        body = stripped[:-1]
    return body + eol


class Applier:
    def __init__(self, inv: Inventory, code_dir: Path, emit=null_emit) -> None:
        self.inv = inv
        self.code_dir = code_dir
        self.emit = emit
        self.stats = ApplyStats()

    def run(self, findings: List[Finding], write: bool = True) -> ApplyStats:
        removals = [f for f in findings if f.decision == "remove" and f.status == "pending" and f.edit]
        deleted_files: Set[str] = set()

        # 1. Whole paths (directories first, so nested items are reported as included).
        def path_order(f: Finding) -> Tuple[int, str]:
            return (0 if f.path in self.inv.dirs else 1, f.path)

        for f in sorted([x for x in removals if x.edit["op"] in ("delete_path", "delete_paths")], key=path_order):
            if f.edit["op"] == "delete_paths":
                rels = [r for r in f.edit.get("paths", []) if r in self.inv.by_rel]
            elif f.path in self.inv.dirs:
                rels = [x.rel for x in self.inv.under(f.path)]
            else:
                rels = [f.path] if f.path in self.inv.by_rel else []
            new = [r for r in rels if r not in deleted_files]
            if rels and not new:
                f.status, f.status_detail = "superseded", "ya incluido en otra eliminación"
                continue
            if not rels:
                f.status, f.status_detail = "failed", "la ruta ya no existe"
                continue
            deleted_files.update(new)
            f.status = "removed"
            f.removed_files = len(new)
            if f.path in self.inv.dirs:
                self.stats.dirs_deleted += 1
                self.emit("removed", f"{f.path}/ — {f.reason} ({len(new)} ficheros)", path=f.path, files=len(new))
            elif f.edit["op"] == "delete_paths":
                self.emit("removed", f"{f.path} — {f.reason} ({len(new)} ficheros)", path=f.path, files=len(new))
            else:
                self.emit("removed", f"{f.path} — {f.reason}", path=f.path, files=1)
        self.stats.files_deleted = len(deleted_files)

        # 2. Line edits inside removed files are no longer needed.
        line_edits = [f for f in removals if f.status == "pending" and f.edit["op"] == "delete_lines"]
        for f in line_edits:
            if f.path in deleted_files:
                f.status, f.status_detail = "superseded", "el fichero completo se elimina"

        # 3. Copy the project without the deleted files.
        self.stats.bytes_before = sum(fi.size for fi in self.inv.files)
        if write:
            if self.code_dir.exists():
                shutil.rmtree(self.code_dir)
            self.code_dir.mkdir(parents=True)
            for fi in self.inv.files:
                if fi.rel in deleted_files:
                    continue
                target = self.code_dir / fi.rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fi.path, target)

        # 4. Line edits, file by file.
        by_path: Dict[str, List[Finding]] = defaultdict(list)
        for f in line_edits:
            if f.status == "pending":
                by_path[f.path].append(f)
        for rel, items in sorted(by_path.items()):
            self._edit_file(rel, items, write)

        if write:
            self.stats.bytes_after = sum(p.stat().st_size for p in self.code_dir.rglob("*") if p.is_file())
        return self.stats

    def _edit_file(self, rel: str, items: List[Finding], write: bool) -> None:
        source = (self.code_dir / rel) if write else self.inv.by_rel[rel].path
        try:
            original = source.read_bytes()
        except OSError:
            for f in items:
                f.status, f.status_detail = "failed", "no se pudo leer el fichero"
            return
        lines = split_lines_b(original)
        text_lines = split_lines(decode(original)[0])
        remove: Set[int] = set()
        strip_comma: Set[int] = set()
        applied: List[Tuple[Finding, int]] = []
        for f in sorted(items, key=lambda x: x.start_line or 0):
            ranges = f.edit.get("ranges") or []
            ok = bool(ranges) and all(1 <= s <= e <= len(lines) for s, e in ranges)
            expect = f.edit.get("expect")
            if ok and expect is not None:
                first, last = expect.get("first", ""), expect.get("last", "")
                s, e = ranges[0][0], ranges[-1][1]
                ok = bool(first and last) and norm_ws(text_lines[s - 1]) == norm_ws(first) \
                    and norm_ws(text_lines[e - 1]) == norm_ws(last)
            if not ok:
                f.status, f.status_detail = "failed", "las líneas indicadas no coinciden con el fichero"
                continue
            new_lines = {n for s, e in ranges for n in range(s, e + 1)} - remove
            remove.update(new_lines)
            strip_comma.update(f.edit.get("strip_trailing_comma", []))
            applied.append((f, len(new_lines)))
        if not applied:
            return
        out = []
        for number, line in enumerate(lines, start=1):
            if number in remove:
                continue
            out.append(_strip_comma(line) if number in strip_comma else line)
        result = b"".join(out)
        valid, why = validate(rel, original, result)
        if not valid:
            for f, _ in applied:
                f.status, f.status_detail = "failed", f"revertido: {why}"
            self.stats.reverted.append(rel)
            self.emit("warn", f"{rel}: no se modifica porque {why}", path=rel)
            return
        if write:
            (self.code_dir / rel).write_bytes(result)
        self.stats.files_modified += 1
        self.stats.lines_removed += len(remove)
        comment_blocks, comment_lines = 0, 0
        for f, count in applied:
            f.status = "removed"
            f.removed_lines = count
            if f.category == "commented_code":
                comment_blocks += 1
                comment_lines += count
                continue
            label = f.reason if f.category in ("unreachable_code", "discovered") else (f.symbol or f.reason)
            self.emit("trimmed", f"{f.location()} — {label} ({count} líneas)", path=rel, lines=count)
        if comment_blocks:
            self.emit("trimmed", f"{rel} — {comment_blocks} bloque(s) de código comentado ({comment_lines} líneas)",
                      path=rel, lines=comment_lines)
