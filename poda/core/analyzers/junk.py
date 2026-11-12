"""Junk and build leftovers: dumps, OS/editor files, compiled output, packaged builds."""
from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from typing import Dict, List, Optional, Set

from ..inventory import FileInfo, Inventory
from ...models import Finding, Findings

JUNK_NAMES = {
    "thumbs.db": "caché de miniaturas de Windows",
    "ehthumbs.db": "caché de miniaturas de Windows",
    "desktop.ini": "configuración de carpeta de Windows",
    ".ds_store": "metadatos de carpeta de macOS",
    "nohup.out": "salida de nohup",
}
JUNK_PATTERNS = [
    (re.compile(r"^hs_err_pid\d+\.log$"), "registro de caída de la JVM"),
    (re.compile(r"~$"), "copia de seguridad de editor"),
    (re.compile(r"\.(swp|swo|swn)$"), "fichero temporal de vim"),
    (re.compile(r"\.(bak|old|orig|rej|tmp|temp)$", re.I), "copia de seguridad o fichero temporal"),
    (re.compile(r"\.log$", re.I), "fichero de log"),
    (re.compile(r"\.hprof$", re.I), "volcado de memoria de la JVM"),
    (re.compile(r"\.py[co]$", re.I), "bytecode de Python"),
]
CORE_RE = re.compile(r"^core(\.\d+)?$")
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)


def _magic(fi: FileInfo, size: int = 4) -> bytes:
    try:
        with open(fi.path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def java_source_keys(inv: Inventory) -> Dict[str, str]:
    """'pkg/path/ClassName' -> rel of the .java file that defines it."""
    keys: Dict[str, str] = {}
    for fi in inv.files:
        if fi.ext != "java" or fi.generated:
            continue
        text = inv.text(fi) or ""
        m = PACKAGE_RE.search(text)
        pkg = m.group(1).replace(".", "/") + "/" if m else ""
        keys[pkg + fi.stem] = fi.rel
    return keys


def _class_key_match(parts: List[str], keys: Dict[str, str]) -> Optional[str]:
    stem = parts[-1][:-len(".class")].split("$")[0]
    for i in range(len(parts) - 1):
        key = "/".join(parts[i:-1] + [stem])
        if key in keys:
            return keys[key]
    return keys.get(stem)


def detect_junk(inv: Inventory, findings: Findings) -> None:
    # 1. Build output directories (target/, build/, bin/obj) next to a build file.
    for rel_dir, reason in sorted(inv.build_output_dirs.items()):
        count = len(inv.under(rel_dir))
        f = Finding(category="junk", subtype="build_output", path=rel_dir,
                    reason=f"{reason}: se regenera al compilar",
                    confidence="high", needs_llm=False, detectors=["junk"],
                    evidence=[f"{count} ficheros generados por la compilación"],
                    edit={"op": "delete_path"})
        f.removed_files = count
        findings.add(f)
        inv.junk.update(x.rel for x in inv.under(rel_dir))

    in_output = lambda rel: any(rel.startswith(d + "/") for d in inv.build_output_dirs)

    # 2. Individual junk files.
    for fi in inv.files:
        if in_output(fi.rel) or fi.excluded:
            continue
        low = fi.name.lower()
        reason = JUNK_NAMES.get(low)
        if reason is None and CORE_RE.match(fi.name) and _magic(fi) == b"\x7fELF":
            reason = "volcado de memoria (core dump)"
        if reason is None and fi.ext == "dmp" and _magic(fi) == b"MDMP":
            reason = "volcado de memoria de Windows"
        if reason is None:
            for pattern, why in JUNK_PATTERNS:
                if pattern.search(fi.name):
                    reason = why
                    break
        if reason:
            findings.add(Finding(category="junk", subtype="leftover_file", path=fi.rel,
                                 reason=reason, confidence="high", needs_llm=False,
                                 detectors=["junk"], edit={"op": "delete_path"}))
            inv.junk.add(fi.rel)

    # 3. Compiled classes whose source is in the project; packaged builds of the project.
    keys = java_source_keys(inv)
    with_source: Dict[str, List[str]] = defaultdict(list)
    without_source: Dict[str, List[str]] = defaultdict(list)
    for fi in inv.files:
        if fi.ext != "class" or in_output(fi.rel) or fi.excluded:
            continue
        parts = fi.rel.split("/")
        root = _classes_root(parts)
        if keys and _class_key_match(parts, keys):
            with_source[root].append(fi.rel)
        else:
            without_source[root].append(fi.rel)
    for root, rels in sorted(with_source.items()):
        f = Finding(category="junk", subtype="compiled_classes", path=root or ".",
                    reason="clases compiladas cuyo código fuente está en el proyecto",
                    confidence="high", needs_llm=False, detectors=["junk"],
                    evidence=[f"{len(rels)} ficheros .class con su .java correspondiente"],
                    edit={"op": "delete_paths", "paths": sorted(rels)})
        f.removed_files = len(rels)
        findings.add(f)
        inv.junk.update(rels)
    for root, rels in sorted(without_source.items()):
        findings.add(Finding(category="info", subtype="classes_without_source", path=root or ".",
                             reason=f"{len(rels)} clase(s) compilada(s) sin código fuente en el proyecto: "
                                    "no se tocan (puede ser lógica que solo existe compilada)",
                             confidence="high", needs_llm=False, detectors=["junk"],
                             evidence=sorted(rels)[:5]))

    for fi in inv.files:
        if fi.ext not in ("war", "ear") or in_output(fi.rel) or fi.excluded:
            continue
        own, total = _archive_own_classes(fi, keys)
        if total and own / total >= 0.5:
            findings.add(Finding(category="junk", subtype="packaged_build", path=fi.rel,
                                 reason="paquete desplegable generado a partir del propio código",
                                 confidence="high", needs_llm=False, detectors=["junk"],
                                 evidence=[f"{own} de {total} clases del paquete tienen su .java en el proyecto"],
                                 edit={"op": "delete_path"}))
            inv.junk.add(fi.rel)


def _classes_root(parts: List[str]) -> str:
    for marker in ("classes", "bin", "out"):
        if marker in parts[:-1]:
            idx = len(parts) - 2 - parts[:-1][::-1].index(marker)
            return "/".join(parts[:idx + 1])
    return "/".join(parts[:-1])


def _archive_own_classes(fi: FileInfo, keys: Dict[str, str]) -> tuple:
    if not keys:
        return 0, 0
    try:
        with zipfile.ZipFile(fi.path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".class")]
    except (zipfile.BadZipFile, OSError, ValueError):
        return 0, 0
    own = 0
    for name in names:
        if _class_key_match(name.split("/"), keys):
            own += 1
    return own, len(names)
