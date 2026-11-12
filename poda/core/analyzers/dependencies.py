"""Unused libraries (bundled binaries) and dependencies declared in build files.

JAR analysis is bytecode-aware: a library is used when the project's code or
configuration references its packages or tag-library URIs, or when another
used library needs it (constant-pool references). Everything found here is
verified by the LLM before removal, because runtime-only loading (drivers,
logging back-ends, container libraries) is invisible to static analysis.
"""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..inventory import FileInfo, Inventory
from ...models import Finding, Findings
from ...utils.text import LineMap, split_lines, strip_eol

QUALIFIED_RE = re.compile(r"(?<![\w$.])([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)+)")
SLASHED_RE = re.compile(r"(?<![\w$/])([a-z][\w$]*(?:/[A-Za-z_][\w$]*){2,})")
TLD_URI_RE = re.compile(r"<uri>\s*([^<\s]+)\s*</uri>", re.I)
APP_URI_RE = re.compile(r"""\buri\s*=\s*["']([^"']+)["']|<taglib-uri>\s*([^<\s]+)\s*</taglib-uri>""", re.I)
RUNTIME_RE = re.compile(
    r"(ojdbc|classes12|mysql-connector|mariadb|postgresql|jtds|sqljdbc|mssql-jdbc|db2jcc|jconn|"
    r"^h2-|hsqldb|derby|log4j|logback|slf4j|commons-logging|xerces|xalan|xml-apis|saxon|jaxb|"
    r"activation|mail|jta|jms|wsdl4j|axis|cxf|jersey|resteasy|servlet-api|jsp-api|el-api|jstl|"
    r"standard)", re.I)
VERSION_RE = re.compile(r"^(.*?)[-_](\d[\w.\-]*)$")
DESCRIPTOR_RE = re.compile(rb"L([\w/$]+);")
DOTTED_CLASS_RE = re.compile(rb"^[a-z][\w]*(?:\.[\w$]+){2,}$")

PIP_IMPORTS = {
    "beautifulsoup4": ["bs4"], "pyyaml": ["yaml"], "pillow": ["PIL"], "scikit-learn": ["sklearn"],
    "python-dateutil": ["dateutil"], "opencv-python": ["cv2"], "mysqlclient": ["MySQLdb"],
    "psycopg2-binary": ["psycopg2"], "protobuf": ["google"], "attrs": ["attr"],
    "typing-extensions": ["typing_extensions"], "python-dotenv": ["dotenv"], "pyjwt": ["jwt"],
    "pymongo": ["pymongo", "bson", "gridfs"], "pyserial": ["serial"], "pycryptodome": ["Crypto"],
    "msgpack-python": ["msgpack"], "python-ldap": ["ldap"], "pywin32": ["win32api", "win32com", "pythoncom"],
}
# Maven artifacts whose packages do not follow their groupId (used when the jar
# itself is not available in the project, in a packaged .war/.ear or in ~/.m2).
ARTIFACT_PACKAGES = {
    "oro": ["org.apache.oro"], "jcommon": ["org.jfree"], "jfreechart": ["org.jfree.chart"],
    "log4j": ["org.apache.log4j"], "log4j-core": ["org.apache.logging.log4j"],
    "log4j-api": ["org.apache.logging.log4j"], "struts": ["org.apache.struts"],
    "struts-core": ["org.apache.struts"], "struts-legacy": ["org.apache.struts.legacy"],
    "struts-menu": ["net.sf.navigator"], "displaytag": ["org.displaytag"],
    "jstl": ["javax.servlet.jsp.jstl"], "standard": ["org.apache.taglibs.standard"],
    "servlet-api": ["javax.servlet"], "jsp-api": ["javax.servlet.jsp"], "j2ee": ["javax"],
    "poi": ["org.apache.poi"], "itext": ["com.lowagie.text", "com.itextpdf"], "dom4j": ["org.dom4j"],
    "jdom": ["org.jdom"], "xercesimpl": ["org.apache.xerces"], "xml-apis": ["org.w3c.dom", "javax.xml"],
    "jackson-databind": ["com.fasterxml.jackson.databind"], "jackson-core": ["com.fasterxml.jackson.core"],
    "jackson-annotations": ["com.fasterxml.jackson.annotation"], "httpclient": ["org.apache.http"],
    "httpcore": ["org.apache.http"], "httpclient5": ["org.apache.hc.client5"],
    "httpcore5": ["org.apache.hc.core5"], "gson": ["com.google.gson"], "guava": ["com.google.common"],
    "junit": ["junit", "org.junit"], "hsqldb": ["org.hsqldb"], "mysql-connector-java": ["com.mysql"],
    "postgresql": ["org.postgresql"], "aopalliance": ["org.aopalliance"], "aspectjweaver": ["org.aspectj"],
    "aspectjrt": ["org.aspectj"], "slf4j-api": ["org.slf4j"], "hibernate-core": ["org.hibernate"],
    "commons-email": ["org.apache.commons.mail"], "xstream": ["com.thoughtworks.xstream"],
    "quartz": ["org.quartz"], "velocity": ["org.apache.velocity"], "freemarker": ["freemarker"],
    "axis": ["org.apache.axis"], "jaxrpc": ["javax.xml.rpc"], "mail": ["javax.mail"],
    "activation": ["javax.activation"],
}

VB_LIBRARIES = {
    "activex data objects": ["ADODB"], "scripting runtime": ["Scripting", "FileSystemObject", "Dictionary"],
    "microsoft xml": ["MSXML2", "DOMDocument", "XMLHTTP"], "excel": ["Excel"], "word": ["Word"],
    "dao": ["DAO", "DBEngine"], "outlook": ["Outlook"], "remote data object": ["rdo", "RDO"],
    "cdo": ["CDO"], "crystal": ["CRAXDRT", "CRPEAuto", "CrystalReport"], "common dialog": ["CommonDialog"],
    "windows common controls": ["MSComctlLib", "ListView", "TreeView", "ProgressBar"],
    "internet transfer": ["Inet"], "winsock": ["Winsock"], "flexgrid": ["MSFlexGrid", "MSHFlexGrid"],
}


# ---------------------------------------------------------------------------
# Class files
# ---------------------------------------------------------------------------

def class_refs(data: bytes) -> Tuple[Optional[str], Set[str]]:
    """Internal name of a class and the internal names it references."""
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return None, set()
    count = int.from_bytes(data[8:10], "big")
    i, idx = 10, 1
    utf8: Dict[int, bytes] = {}
    class_entries: Dict[int, int] = {}
    try:
        while idx < count:
            tag = data[i]
            if tag == 1:
                length = int.from_bytes(data[i + 1:i + 3], "big")
                utf8[idx] = data[i + 3:i + 3 + length]
                i += 3 + length
            elif tag in (3, 4):
                i += 5
            elif tag in (5, 6):
                i += 9
                idx += 1
            elif tag == 7:
                class_entries[idx] = int.from_bytes(data[i + 1:i + 3], "big")
                i += 3
            elif tag in (8, 16, 19, 20):
                i += 3
            elif tag in (9, 10, 11, 12, 17, 18):
                i += 5
            elif tag == 15:
                i += 4
            else:
                break
            idx += 1
        this_idx = int.from_bytes(data[i + 2:i + 4], "big")
    except IndexError:
        return None, set()
    this_name = None
    if this_idx in class_entries:
        this_name = utf8.get(class_entries[this_idx], b"").decode("utf-8", "replace")
    refs: Set[str] = set()
    for name_idx in class_entries.values():
        raw = utf8.get(name_idx, b"")
        if raw.startswith(b"["):
            refs.update(m.decode("utf-8", "replace") for m in DESCRIPTOR_RE.findall(raw))
        elif raw:
            refs.add(raw.decode("utf-8", "replace"))
    for raw in utf8.values():
        if b";" in raw:
            refs.update(m.decode("utf-8", "replace") for m in DESCRIPTOR_RE.findall(raw))
        elif DOTTED_CLASS_RE.match(raw):
            refs.add(raw.decode("utf-8", "replace").replace(".", "/"))
    return this_name, refs


def _package(internal: str) -> Optional[str]:
    return internal.rsplit("/", 1)[0].replace("/", ".").lower() if "/" in internal else None


@dataclass
class JarInfo:
    rel: str
    name: str
    packages: Set[str] = field(default_factory=set)
    classes: Set[str] = field(default_factory=set)
    ref_packages: Set[str] = field(default_factory=set)
    tld_uris: Set[str] = field(default_factory=set)
    services: Set[str] = field(default_factory=set)
    error: Optional[str] = None

    @property
    def runtime(self) -> bool:
        return bool(RUNTIME_RE.search(self.name)) or any(
            s.startswith(("java.sql.", "javax.xml.", "org.apache.commons.logging", "javax.servlet."))
            for s in self.services)


def analyse_jar(source, rel: str) -> JarInfo:
    """Analyse a jar given as a path or as raw bytes (jars nested in a .war/.ear)."""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    info = JarInfo(rel=rel, name=name)
    try:
        with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as zf:
            for entry in zf.infolist():
                name = entry.filename
                if name.endswith(".class") and not name.startswith("META-INF/"):
                    this_name, refs = class_refs(zf.read(entry))
                    internal = this_name or name[:-6]
                    pkg = _package(internal)
                    if pkg:
                        info.packages.add(pkg)
                    info.classes.add(internal.replace("/", "."))
                    for ref in refs:
                        ref_pkg = _package(ref)
                        if ref_pkg and not ref_pkg.startswith("java."):
                            info.ref_packages.add(ref_pkg)
                elif name.lower().endswith(".tld"):
                    text = zf.read(entry).decode("utf-8", "replace")
                    info.tld_uris.update(u.strip() for u in TLD_URI_RE.findall(text))
                elif name.startswith("META-INF/services/") and not name.endswith("/"):
                    info.services.add(name.rsplit("/", 1)[-1])
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as exc:
        info.error = str(exc)
    info.ref_packages -= info.packages
    return info


# ---------------------------------------------------------------------------
# Project-wide references
# ---------------------------------------------------------------------------

@dataclass
class AppRefs:
    prefixes: Set[str]
    uris: Set[str]


def app_references(inv: Inventory) -> AppRefs:
    prefixes: Set[str] = set()
    uris: Set[str] = set()
    for fi in inv.files:
        if not inv.is_referrer(fi):
            continue
        text = inv.text(fi)
        if not text:
            continue
        for m in QUALIFIED_RE.finditer(text):
            parts = m.group(1).lower().split(".")
            for k in range(2, len(parts) + 1):
                prefixes.add(".".join(parts[:k]))
        for m in SLASHED_RE.finditer(text):
            parts = m.group(1).lower().split("/")
            for k in range(2, len(parts) + 1):
                prefixes.add(".".join(parts[:k]))
        for m in APP_URI_RE.finditer(text):
            uris.add((m.group(1) or m.group(2) or "").strip())
    return AppRefs(prefixes, uris)


def _line_range(text: str, start: int, end: int) -> Optional[Tuple[int, int]]:
    """Whole-line range covering text[start:end] if nothing else shares those lines."""
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        return None
    nl = text.find("\n", end)
    tail = text[end:nl if nl >= 0 else len(text)]
    if tail.strip():
        return None
    lmap = LineMap(text)
    return lmap.line(start), lmap.line(max(start, end - 1))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class DependencyDetector:
    def __init__(self, inv: Inventory, findings: Findings) -> None:
        self.inv = inv
        self.findings = findings
        self.refs = app_references(inv)
        self.jars: Dict[str, JarInfo] = {}           # removable jars of the project
        self.archive_jars: Dict[str, JarInfo] = {}   # jars inside .war/.ear: lookup only
        self.used_jars: Set[str] = set()
        self.jar_classes: Dict[str, str] = {}

    def run(self) -> None:
        self._jars()
        self._maven()
        self._npm()
        self._pip()
        self._vbp()
        self._dotnet()
        self._gradle()

    # -- bundled jars ---------------------------------------------------------
    def _jars(self) -> None:
        for fi in self.inv.files:
            if fi.ext == "jar" and not fi.generated and not fi.excluded and fi.rel not in self.inv.junk:
                self.jars[fi.rel] = analyse_jar(fi.path, fi.rel)
            elif fi.ext in ("war", "ear") and not fi.excluded:
                # Packaged builds carry the real dependency jars: they tell which
                # packages each Maven artifact provides and which library needs which.
                try:
                    with zipfile.ZipFile(fi.path) as zf:
                        for name in zf.namelist():
                            if name.lower().endswith(".jar"):
                                key = f"{fi.rel}!/{name}"
                                self.archive_jars[key] = analyse_jar(zf.read(name), key)
                except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
                    pass
        everything = {**self.archive_jars, **self.jars}
        for jar in everything.values():
            for cls in jar.classes:
                self.jar_classes.setdefault(cls, jar.name)
            if jar.packages & self.refs.prefixes or jar.tld_uris & self.refs.uris:
                self.used_jars.add(jar.rel)
        changed = True
        while changed:
            changed = False
            for user in [everything[r] for r in list(self.used_jars)]:
                for jar in everything.values():
                    if jar.rel not in self.used_jars and jar.packages & user.ref_packages:
                        self.used_jars.add(jar.rel)
                        changed = True
        for jar in sorted(self.jars.values(), key=lambda j: j.rel):
            if jar.error or jar.rel in self.used_jars:
                continue
            evidence = []
            if jar.packages:
                sample = ", ".join(sorted(jar.packages, key=lambda p: (len(p), p))[:4])
                evidence.append(f"Ningún fichero del proyecto referencia sus paquetes ({sample}...)")
                evidence.append("Ninguna otra librería en uso necesita sus clases (análisis de bytecode)")
            else:
                evidence.append("No contiene clases (solo recursos o código fuente)")
            if jar.tld_uris:
                evidence.append("Sus URIs de taglib no se usan: " + ", ".join(sorted(jar.tld_uris)[:3]))
            confidence = "medium"
            if jar.runtime:
                confidence = "low"
                evidence.append("Tipo de librería que suele cargarse solo en tiempo de ejecución "
                                "(driver, logging, parser XML o API del contenedor)")
            self.findings.add(Finding(
                category="unused_library", subtype="jar", path=jar.rel, symbol=jar.name,
                reason="librería incluida en el proyecto que no se usa",
                confidence=confidence, needs_llm=True, detectors=["dependencies"],
                evidence=evidence, edit={"op": "delete_path"}))
        groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for jar in self.jars.values():
            stem = jar.name[:-4]
            m = VERSION_RE.match(stem)
            base = (m.group(1) if m else stem).lower()
            groups[(jar.rel.rsplit("/", 1)[0] if "/" in jar.rel else "", base)].append(jar.rel)
        for (folder, base), rels in sorted(groups.items()):
            if len(rels) > 1:
                self.findings.add(Finding(
                    category="duplicate_library", subtype="duplicate_jar", path=folder or ".",
                    symbol=base, reason=f"varias versiones de la misma librería ({base}): "
                                        "en el classpath solo gana una, revisar cuál sobra",
                    confidence="high", needs_llm=False, detectors=["dependencies"],
                    evidence=sorted(rels)))

    def _jar_is_used(self, jar: JarInfo) -> bool:
        if jar.rel in self.used_jars:
            return True
        if jar.packages & self.refs.prefixes or jar.tld_uris & self.refs.uris:
            return True
        everything = {**self.archive_jars, **self.jars}
        return any(jar.packages & everything[r].ref_packages for r in self.used_jars if r in everything)

    # -- Maven ----------------------------------------------------------------
    def _maven(self) -> None:
        for fi in self.inv.files:
            if fi.name != "pom.xml" or fi.generated or fi.excluded:
                continue
            text = self.inv.text(fi)
            if not text:
                continue
            try:
                root = ET.fromstring(fi.path.read_bytes())
            except (ET.ParseError, OSError):
                continue
            props = self._pom_properties(root)
            deps_node = _child(root, "dependencies")
            if deps_node is None:
                continue
            blocked = [(m.start(), m.end()) for m in re.finditer(
                r"(?s)<dependencyManagement>.*?</dependencyManagement>|<plugins>.*?</plugins>|"
                r"<profiles>.*?</profiles>", text)]
            for dep in _children(deps_node, "dependency"):
                group = _resolve(_text(dep, "groupId"), props)
                artifact = _resolve(_text(dep, "artifactId"), props)
                version = _resolve(_text(dep, "version"), props)
                scope = (_text(dep, "scope") or "compile").lower()
                if not artifact or scope == "import":
                    continue
                verdict = self._artifact_usage(group, artifact, version)
                if verdict is None:
                    continue
                confidence, evidence = verdict
                if scope == "runtime":
                    confidence = "low"
                    evidence.append("Ámbito runtime: se usa solo al ejecutar, no al compilar")
                edit = None
                for m in re.finditer(r"(?s)<dependency>(.*?)</dependency>", text):
                    if any(a <= m.start() < b for a, b in blocked):
                        continue
                    body = m.group(1)
                    if re.search(r"<artifactId>\s*" + re.escape(_raw_text(dep, "artifactId")) + r"\s*</artifactId>", body) \
                            and re.search(r"<groupId>\s*" + re.escape(_raw_text(dep, "groupId")) + r"\s*</groupId>", body):
                        rng = _line_range(text, m.start(), m.end())
                        if rng:
                            edit = {"op": "delete_lines", "ranges": [list(rng)]}
                        break
                self.findings.add(Finding(
                    category="unused_dependency", subtype="maven", path=fi.rel,
                    start_line=edit["ranges"][0][0] if edit else None,
                    end_line=edit["ranges"][0][1] if edit else None,
                    symbol=f"{group}:{artifact}" + (f":{version}" if version else ""),
                    reason="dependencia Maven declarada que no se usa",
                    confidence=confidence, needs_llm=True, detectors=["dependencies"],
                    evidence=evidence + [f"scope: {scope}"], edit=edit))

    @staticmethod
    def _pom_properties(root: ET.Element) -> Dict[str, str]:
        props: Dict[str, str] = {}
        node = _child(root, "properties")
        if node is not None:
            for child in list(node):
                props[_local(child.tag)] = (child.text or "").strip()
        for key in ("version", "groupId", "artifactId"):
            value = _text(root, key)
            if value:
                props[f"project.{key}"] = value
                props[f"pom.{key}"] = value
        return props

    def _artifact_usage(self, group: str, artifact: str, version: str) -> Optional[Tuple[str, List[str]]]:
        """None if used; otherwise (confidence, evidence)."""
        jar = None
        where = ""
        for info in list(self.jars.values()) + list(self.archive_jars.values()):
            stem = info.name[:-4].lower()
            if stem == artifact.lower() or stem.startswith(artifact.lower() + "-"):
                jar = info
                where = f" (dentro de {info.rel.split('!/')[0]})" if "!/" in info.rel else ""
                break
        if jar is None and group and artifact and version:
            m2 = Path(os.path.expanduser("~")) / ".m2" / "repository" / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.jar"
            if m2.is_file():
                jar = analyse_jar(m2, str(m2))
                where = " (en ~/.m2)"
        if jar is not None and not jar.error:
            if self._jar_is_used(jar):
                return None
            sample = ", ".join(sorted(jar.packages, key=lambda p: (len(p), p))[:3]) or "sin clases"
            return ("medium" if not jar.runtime else "low",
                    [f"Analizado {jar.name}{where}: ningún fichero ni librería en uso referencia sus paquetes ({sample})"])
        art, group_l = artifact.lower(), (group or "").lower()
        packages = list(ARTIFACT_PACKAGES.get(art, []))
        if art.startswith("commons-"):
            packages.append("org.apache.commons." + art[len("commons-"):].replace("-", ""))
        if art.startswith("spring-"):
            packages.append("org.springframework")
        if group_l:
            packages.append(group_l)
            if group_l.count(".") >= 2:
                packages.append(group_l.rsplit(".", 1)[0])
        for pkg in packages:
            if pkg in self.refs.prefixes or any(p.startswith(pkg + ".") for p in self.refs.prefixes):
                return None
        return ("low", ["No se ha podido inspeccionar el artefacto (no está en el proyecto, en un .war/.ear "
                        f"ni en ~/.m2); ninguno de estos paquetes aparece en el código: {', '.join(packages[:4])}"])

    # -- npm --------------------------------------------------------------------
    def _npm(self) -> None:
        js_texts = None
        for fi in self.inv.files:
            if fi.name != "package.json" or fi.generated or fi.excluded:
                continue
            text = self.inv.text(fi)
            try:
                data = json.loads(text or "")
            except ValueError:
                continue
            if js_texts is None:
                js_texts = [self.inv.text(f) or "" for f in self.inv.files
                            if self.inv.is_referrer(f) and f.ext in ("js", "mjs", "cjs", "jsx", "ts", "tsx",
                                                                     "vue", "html", "htm", "jsp", "json", "cjs")
                            and f.name != "package.json"]
                js_texts += [self.inv.text(f) or "" for f in self.inv.files if self.inv.is_referrer(f)
                             and re.match(r"^(\.babelrc|\.eslintrc.*|webpack.*|gulpfile.*|gruntfile.*|karma\.conf.*|"
                                          r"jest\.config.*|postcss\.config.*|tsconfig.*|\.browserslistrc)$", f.name, re.I)]
            scripts = json.dumps(data.get("scripts", {}))
            lines = split_lines(text)
            for section in ("dependencies", "devDependencies"):
                for name in (data.get(section) or {}):
                    if name.startswith("@types/") or self._npm_used(name, js_texts, scripts):
                        continue
                    edit = _json_entry_edit(lines, section, name)
                    self.findings.add(Finding(
                        category="unused_dependency", subtype="npm", path=fi.rel, symbol=name,
                        start_line=edit["ranges"][0][0] if edit else None,
                        end_line=edit["ranges"][0][1] if edit else None,
                        reason=f"paquete npm de {section} que no se usa",
                        confidence="medium", needs_llm=True, detectors=["dependencies"],
                        evidence=[f"Ningún require/import de '{name}' ni uso en scripts o configuración"],
                        edit=edit))

    @staticmethod
    def _npm_used(name: str, texts: List[str], scripts: str) -> bool:
        pat = re.compile(r"""(?:require\s*\(\s*|from\s+|import\s*\(?\s*)['"]""" + re.escape(name) + r"""(?:/[^'"]*)?['"]""")
        short = name.split("/")[-1]
        for prefix in ("babel-plugin-", "babel-preset-", "eslint-plugin-", "eslint-config-", "@babel/plugin-", "@babel/preset-"):
            if short.startswith(prefix.split("/")[-1]):
                short = short[len(prefix.split("/")[-1]):] or short
        word = re.compile(r"(?<![\w-])" + re.escape(short) + r"(?![\w-])")
        if word.search(scripts) or re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", scripts):
            return True
        for text in texts:
            if pat.search(text) or name in text and ("plugins" in text or "presets" in text or "extends" in text):
                return True
        return False

    # -- pip ----------------------------------------------------------------------
    def _pip(self) -> None:
        py_texts = None
        for fi in self.inv.files:
            if not re.match(r"requirements.*\.txt$", fi.name.lower()) or fi.generated or fi.excluded:
                continue
            text = self.inv.text(fi) or ""
            if py_texts is None:
                py_texts = [self.inv.text(f) or "" for f in self.inv.files if f.ext == "py" and self.inv.is_referrer(f)]
            if not py_texts:
                return
            for idx, raw in enumerate(split_lines(text), start=1):
                line = strip_eol(raw).split("#", 1)[0].strip()
                m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                if not m or line.startswith("-"):
                    continue
                dist = m.group(1)
                modules = PIP_IMPORTS.get(dist.lower(), [dist.lower().replace("-", "_").replace(".", "_")])
                pattern = re.compile(r"(?m)^\s*(?:import|from)\s+(?:" + "|".join(re.escape(x) for x in modules) + r")(?:\.|\s|,|$)", re.I)
                if any(pattern.search(t) for t in py_texts):
                    continue
                self.findings.add(Finding(
                    category="unused_dependency", subtype="pip", path=fi.rel, symbol=dist,
                    start_line=idx, end_line=idx, reason="paquete de Python declarado que no se importa",
                    confidence="medium", needs_llm=True, detectors=["dependencies"],
                    evidence=[f"Ningún import de {', '.join(modules)} en los ficheros .py"],
                    edit={"op": "delete_lines", "ranges": [[idx, idx]]}))

    # -- Visual Basic 6 -----------------------------------------------------------
    def _vbp(self) -> None:
        vb_files = [f for f in self.inv.files if f.ext in ("frm", "ctl", "dsr", "pag", "bas", "cls") and self.inv.is_referrer(f)]
        texts = {f.rel: self.inv.text(f) or "" for f in vb_files}
        forms_text = "\n".join(t for r, t in texts.items() if r.lower().endswith((".frm", ".ctl", ".dsr", ".pag")))
        code_text = "\n".join(texts.values())
        for fi in self.inv.files:
            if fi.ext != "vbp" or fi.generated or fi.excluded:
                continue
            for idx, raw in enumerate(split_lines(self.inv.text(fi) or ""), start=1):
                line = strip_eol(raw)
                if line.lower().startswith("object="):
                    guid = re.search(r"\{[0-9A-Fa-f-]{36}\}", line)
                    ocx = line.rsplit(";", 1)[-1].strip()
                    if guid and guid.group(0).lower() not in forms_text.lower():
                        self._add_simple(fi, idx, "vb6_object", ocx,
                                         f"control ActiveX ({ocx}) que ningún formulario usa",
                                         [f"El GUID {guid.group(0)} no aparece en ningún .frm/.ctl/.dsr"], "medium")
                elif line.lower().startswith("reference="):
                    desc = line.rsplit("#", 1)[-1].strip()
                    prefixes = next((v for k, v in VB_LIBRARIES.items() if k in desc.lower()), None)
                    if not prefixes:
                        continue
                    if any(re.search(r"(?<![\w])" + re.escape(p) + r"(?![\w])", code_text, re.I) for p in prefixes):
                        continue
                    self._add_simple(fi, idx, "vb6_reference", desc,
                                     f"referencia COM ({desc}) sin uso en el código",
                                     [f"Ninguno de {', '.join(prefixes)} aparece en el código VB"], "low")

    # -- .NET ---------------------------------------------------------------------
    def _dotnet(self) -> None:
        texts = None
        for fi in self.inv.files:
            if not (fi.name.lower() == "packages.config" or fi.ext in ("csproj", "vbproj")) or fi.generated or fi.excluded:
                continue
            if texts is None:
                texts = "\n".join(self.inv.text(f) or "" for f in self.inv.files
                                  if self.inv.is_referrer(f) and f.ext in ("cs", "vb", "cshtml", "vbhtml", "aspx",
                                                                           "ascx", "master", "config", "asax"))
            for idx, raw in enumerate(split_lines(self.inv.text(fi) or ""), start=1):
                line = strip_eol(raw)
                m = re.search(r'<(?:PackageReference|package)\s+(?:Include|id)="([^"]+)"', line)
                if not m or not line.rstrip().endswith(("/>", "</PackageReference>")):
                    continue
                package = m.group(1)
                root_ns = package.split(".")[0]
                if re.search(re.escape(package), texts, re.I) or re.search(r"\b" + re.escape(root_ns) + r"\.", texts):
                    continue
                self._add_simple(fi, idx, "nuget", package, f"paquete NuGet ({package}) sin uso aparente",
                                 [f"Ni '{package}' ni el espacio de nombres '{root_ns}.' aparecen en el código"], "low")

    # -- Gradle -------------------------------------------------------------------
    def _gradle(self) -> None:
        for fi in self.inv.files:
            if fi.name not in ("build.gradle", "build.gradle.kts") or fi.generated or fi.excluded:
                continue
            for idx, raw in enumerate(split_lines(self.inv.text(fi) or ""), start=1):
                m = re.match(r"""^\s*(implementation|api|compile|compileOnly|runtimeOnly|runtime|testImplementation|testCompile)\s*\(?\s*['"]([\w.\-]+):([\w.\-]+)(?::([^'"]+))?['"]\s*\)?\s*$""", strip_eol(raw))
                if not m:
                    continue
                group, artifact, version = m.group(2), m.group(3), m.group(4) or ""
                verdict = self._artifact_usage(group, artifact, version)
                if verdict is None:
                    continue
                confidence, evidence = verdict
                if m.group(1) in ("runtimeOnly", "runtime"):
                    confidence = "low"
                self._add_simple(fi, idx, "gradle", f"{group}:{artifact}", "dependencia Gradle declarada que no se usa",
                                 evidence, confidence)

    def _add_simple(self, fi: FileInfo, idx: int, subtype: str, symbol: str, reason: str,
                    evidence: List[str], confidence: str) -> None:
        self.findings.add(Finding(
            category="unused_dependency", subtype=subtype, path=fi.rel, symbol=symbol,
            start_line=idx, end_line=idx, reason=reason, confidence=confidence, needs_llm=True,
            detectors=["dependencies"], evidence=evidence,
            edit={"op": "delete_lines", "ranges": [[idx, idx]]}))


# ---------------------------------------------------------------------------
# XML / JSON helpers
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in list(node):
        if _local(child.tag) == name:
            return child
    return None


def _children(node: ET.Element, name: str) -> Iterable[ET.Element]:
    return [c for c in list(node) if _local(c.tag) == name]


def _raw_text(node: ET.Element, name: str) -> str:
    child = _child(node, name)
    return (child.text or "").strip() if child is not None else ""


def _text(node: ET.Element, name: str) -> str:
    return _raw_text(node, name)


def _resolve(value: str, props: Dict[str, str]) -> str:
    for _ in range(5):
        new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def _json_entry_edit(lines: List[str], section: str, name: str) -> Optional[dict]:
    """Line edit removing `"name": ...` from a JSON object, fixing the comma."""
    in_section = False
    depth = 0
    for idx, raw in enumerate(lines, start=1):
        line = strip_eol(raw)
        if not in_section:
            if re.match(r'^\s*"' + re.escape(section) + r'"\s*:\s*\{\s*$', line):
                in_section, depth = True, 1
            continue
        if re.match(r'^\s*\}', line) and depth == 1:
            return None
        if re.match(r'^\s*"' + re.escape(name) + r'"\s*:\s*"[^"]*"\s*,?\s*$', line):
            edit = {"op": "delete_lines", "ranges": [[idx, idx]]}
            if not line.rstrip().endswith(","):
                k = idx - 1
                while k >= 1 and not strip_eol(lines[k - 1]).strip():
                    k -= 1
                prev = strip_eol(lines[k - 1]).rstrip() if k >= 1 else ""
                if prev.endswith(","):
                    edit["strip_trailing_comma"] = [k]
            return edit
    return None
