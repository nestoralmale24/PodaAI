"""Project inventory: walk, classify, decode and index the files (read-only)."""
from __future__ import annotations

import fnmatch
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..utils.text import EXT_SYNTAX, decode, is_binary


def _exts(names: str) -> Set[str]:
    return set(names.split())


SOURCE_EXT = _exts(
    "java kt kts scala groovy cs vb bas cls frm ctl dsr js mjs cjs jsx ts tsx py rb php php3 php4 "
    "php5 phtml go c h cpp hpp cc cxx hh m swift pas dpr dpk pp lpr cbl cob cpy pco ccp cobol asp "
    "aspx ascx master ashx asmx cshtml vbhtml vbs pl pm lua r as xaml")
WEB_EXT = _exts("jsp jspf jspx tag tagx html htm xhtml shtml css scss less vm ftl xsl xslt")
SCRIPT_EXT = _exts("sh bash ksh zsh csh bat cmd ps1 jcl rexx awk")
DB_EXT = _exts("sql ddl pls plsql pkb pks prc fnc trg tps tpb")
CONFIG_EXT = _exts("xml xsd dtd wsdl tld properties yml yaml json ini conf cfg toml config env mf "
                   "manifest hbm jrxml")
DOC_EXT = _exts("md markdown rst txt adoc asciidoc doc docx pdf odt rtf xls xlsx ods ppt pptx csv "
                "vsd vsdx")
RESOURCE_EXT = _exts("png gif jpg jpeg bmp ico svg webp tif tiff ttf otf woff woff2 eot mp3 wav ogg "
                     "avi mp4 mov swf flv frx ctx cur ani wmf emf psd")
LIBRARY_EXT = _exts("jar dll ocx so dylib lib a tlb olb bpl aar")
ARCHIVE_EXT = _exts("war ear sar zip gz tgz tar rar 7z bz2 xz")
BINARY_EXT = _exts("class exe obj o pyc pyo pdb dcu res msi ncb suo")
NEVER_TEXT = RESOURCE_EXT - {"svg"} | LIBRARY_EXT | ARCHIVE_EXT | BINARY_EXT | _exts(
    "doc docx pdf odt xls xlsx ods ppt pptx vsd vsdx")

BUILD_NAMES = {
    "pom.xml", "build.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "gradle.properties", "package.json", "makefile", "gnumakefile",
    "cmakelists.txt", "setup.py", "setup.cfg", "pyproject.toml", "pipfile", "composer.json",
    "gemfile", "go.mod", "ivy.xml", "ivysettings.xml", ".classpath", ".project", "packages.config",
}
BUILD_SUFFIXES = (".csproj", ".vbproj", ".sln", ".vbp", ".vbw", ".vbg", ".dproj", ".bdsproj",
                  ".cbproj", ".iml", ".dsp", ".dsw", ".vcxproj", ".mak")
DOC_NAMES = {"readme", "license", "licence", "changelog", "changes", "notice", "authors", "copying",
             "history", "todo"}

VCS_DIRS = {".git", ".svn", ".hg", ".bzr", "CVS", "_darcs"}
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".tox", ".gradle", ".sass-cache",
              ".nyc_output", ".scannerwork"}
IDE_DIRS = {".idea", ".vscode", ".settings", "nbproject", ".metadata", ".vs"}

VENDOR_DIRS = {"vendor", "vendors", "third_party", "thirdparty", "third-party", "bower_components",
               "external", "externals", "jspm_packages"}
VERSIONED_DIR = re.compile(r"^[A-Za-z][\w.]*?[-_]v?\d+(\.\d+)+([.-]\w+)?$")


def is_vendor(rel: str) -> bool:
    """Third-party code bundled in the project: remove it whole or leave it intact."""
    parts = rel.split("/")
    if any(p.lower() in VENDOR_DIRS or VERSIONED_DIR.match(p) for p in parts[:-1]):
        return True
    name = parts[-1].lower()
    return bool(re.search(r"[.-]min\.(js|css)$", name) or re.match(r"^(jquery|prototype|scriptaculous|mootools|"
                                                                  r"bootstrap|angular|react|vue|lodash|underscore|"
                                                                  r"moment|d3|dojo|ext-all|yui)[\w.-]*\.(js|css)$", name))


TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
COBOL_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]")
FNAME_RE = re.compile(r"[A-Za-z0-9_$][A-Za-z0-9_$.\-]*\.[A-Za-z0-9]{1,6}")

MAX_TEXT_BYTES = 6 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024


@dataclass
class FileInfo:
    rel: str                 # posix path relative to the root
    path: Path
    name: str
    ext: str                 # lowercase, without the dot
    size: int
    kind: str                # source|web|script|db|config|build|doc|resource|library|archive|binary|other
    syntax: Optional[str]    # comment syntax family for text files
    excluded: bool = False   # inside a user-excluded directory: copied untouched, not analysed
    generated: bool = False  # build output or generated documentation
    is_text: Optional[bool] = None
    encoding: Optional[str] = None

    @property
    def parent(self) -> str:
        return self.rel.rsplit("/", 1)[0] if "/" in self.rel else ""

    @property
    def stem(self) -> str:
        return self.name.rsplit(".", 1)[0] if "." in self.name else self.name


def classify(name: str, ext: str) -> str:
    low = name.lower()
    if low in BUILD_NAMES or low.endswith(BUILD_SUFFIXES) or re.match(r"requirements.*\.txt$", low):
        return "build"
    if ext in SOURCE_EXT:
        return "source"
    if ext in WEB_EXT:
        return "web"
    if ext in SCRIPT_EXT:
        return "script"
    if ext in DB_EXT:
        return "db"
    if ext in CONFIG_EXT:
        return "config"
    if ext in DOC_EXT or low.split(".")[0] in DOC_NAMES:
        return "doc"
    if ext in RESOURCE_EXT:
        return "resource"
    if ext in LIBRARY_EXT:
        return "library"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in BINARY_EXT:
        return "binary"
    return "other"


class Inventory:
    def __init__(self, root: Path, exclude_names: Iterable[str] = (),
                 exclude_globs: Iterable[str] = (), out_dir: Optional[Path] = None) -> None:
        self.root = root.resolve()
        self.out_dir = out_dir.resolve() if out_dir else None
        self.exclude_names = {n.strip() for n in exclude_names if n and n.strip()}
        self.exclude_globs = [g.strip().strip("/") for g in exclude_globs if g and g.strip()]
        self.files: List[FileInfo] = []
        self.by_rel: Dict[str, FileInfo] = {}
        self.dirs: Set[str] = set()
        self.skipped: List[Tuple[str, str]] = []        # (rel dir, reason): not copied
        self.build_output_dirs: Dict[str, str] = {}     # rel dir -> reason (junk)
        self.javadoc_dirs: Set[str] = set()
        self.excluded_dirs: Set[str] = set()
        self.junk: Set[str] = set()                     # rels flagged as junk (not referrers)
        self._text: Dict[str, str] = {}
        self._cached_bytes = 0
        self._walk()

    # ------------------------------------------------------------------
    def _skip_reason(self, abs_dir: Path, name: str, parent_files: Set[str]) -> Optional[str]:
        if name in VCS_DIRS:
            return "metadatos de control de versiones"
        if name in (".poda", ".deadcode") or (self.out_dir and abs_dir.resolve() == self.out_dir):
            return "salida de Poda AI"
        if name == "documentation" and (
                (abs_dir / "modules" / "01-modulos-plataforma-json-extracted.json").exists()
                or (abs_dir / "agent-runs").is_dir()):
            return "documentación generada por una ejecución anterior"
        if name == "node_modules" and "package.json" in parent_files:
            return "dependencias npm instaladas (se regeneran con npm install)"
        if name in (".venv", "venv", "env") and (abs_dir / "pyvenv.cfg").exists():
            return "entorno virtual de Python"
        if name in CACHE_DIRS:
            return "cachés regenerables"
        return None

    def _excluded(self, rel: str, name: str) -> bool:
        if name in self.exclude_names or name in IDE_DIRS:
            return True
        return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g + "/*") for g in self.exclude_globs)

    def _walk(self) -> None:
        flags: Dict[str, Tuple[bool, bool]] = {"": (False, False)}   # rel dir -> (excluded, generated)
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=True, followlinks=False):
            abs_dir = Path(dirpath)
            rel_dir = abs_dir.relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            excluded, generated = flags.get(rel_dir, (False, False))
            files_lower = {f.lower() for f in filenames}
            if not generated and any(f in files_lower for f in (
                    "allclasses-frame.html", "allclasses-index.html", "allclasses-noframe.html")):
                generated = True
                self.javadoc_dirs.add(rel_dir)
            keep: List[str] = []
            for d in sorted(dirnames):
                sub_abs = abs_dir / d
                sub_rel = f"{rel_dir}/{d}" if rel_dir else d
                if sub_abs.is_symlink():
                    continue
                reason = self._skip_reason(sub_abs, d, files_lower)
                if reason:
                    self.skipped.append((sub_rel, reason))
                    continue
                sub_excluded = excluded or self._excluded(sub_rel, d)
                if sub_excluded and not excluded:
                    self.excluded_dirs.add(sub_rel)
                sub_generated = generated
                if not generated:
                    out_reason = None
                    if d == "target" and "pom.xml" in files_lower:
                        out_reason = "salida de compilación de Maven (target/)"
                    elif d == "build" and ("build.gradle" in files_lower or "build.gradle.kts" in files_lower):
                        out_reason = "salida de compilación de Gradle (build/)"
                    elif d in ("bin", "obj") and any(f.endswith((".csproj", ".vbproj")) for f in files_lower):
                        out_reason = f"salida de compilación de .NET ({d}/)"
                    if out_reason:
                        self.build_output_dirs[sub_rel] = out_reason
                        sub_generated = True
                flags[sub_rel] = (sub_excluded, sub_generated)
                self.dirs.add(sub_rel)
                keep.append(d)
            dirnames[:] = keep
            for fname in sorted(filenames):
                fpath = abs_dir / fname
                if fpath.is_symlink():
                    continue
                rel = f"{rel_dir}/{fname}" if rel_dir else fname
                ext = fname.rsplit(".", 1)[1].lower() if "." in fname[1:] else ""
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                kind = classify(fname, ext)
                info = FileInfo(rel=rel, path=fpath, name=fname, ext=ext, size=size, kind=kind,
                                syntax=EXT_SYNTAX.get(ext), excluded=excluded,
                                generated=generated or ext == "class")
                self.files.append(info)
                self.by_rel[rel] = info

    # ------------------------------------------------------------------
    def is_text(self, fi: FileInfo) -> bool:
        if fi.is_text is None:
            if fi.ext in NEVER_TEXT or fi.size > MAX_TEXT_BYTES:
                fi.is_text = False
            elif fi.size == 0:
                fi.is_text = True
            else:
                try:
                    with open(fi.path, "rb") as fh:
                        fi.is_text = not is_binary(fh.read(8192))
                except OSError:
                    fi.is_text = False
        return bool(fi.is_text)

    def text(self, fi: FileInfo) -> Optional[str]:
        if fi.rel in self._text:
            return self._text[fi.rel]
        if not self.is_text(fi):
            return None
        try:
            data = fi.path.read_bytes()
        except OSError:
            return None
        text, fi.encoding = decode(data)
        if self._cached_bytes + len(data) <= MAX_CACHE_BYTES:
            self._text[fi.rel] = text
            self._cached_bytes += len(data)
        return text

    def under(self, rel_dir: str) -> List[FileInfo]:
        prefix = rel_dir.rstrip("/") + "/"
        return [f for f in self.files if f.rel.startswith(prefix)]

    def is_referrer(self, fi: FileInfo) -> bool:
        """Can this file keep other files alive by referencing them?"""
        if fi.generated or fi.kind in ("doc", "resource", "library", "archive", "binary"):
            return False
        if fi.rel in self.junk:
            return False
        return self.is_text(fi)

    def dominant_encoding(self, sample: int = 300) -> str:
        counts: Counter = Counter()
        for fi in self.files:
            if fi.kind in ("source", "web", "config") and not fi.generated and self.is_text(fi):
                if fi.encoding is None:
                    self.text(fi)
                if fi.encoding:
                    counts[fi.encoding] += 1
                if sum(counts.values()) >= sample:
                    break
        if not counts:
            return "utf-8"
        enc = counts.most_common(1)[0][0]
        return "utf-8" if enc == "utf-8-sig" else enc

    # ------------------------------------------------------------------
    def summary_markdown(self) -> str:
        live = [f for f in self.files if not f.generated]
        kinds = Counter(f.kind for f in live)
        exts = Counter((f".{f.ext}" if f.ext else "(no extension)") for f in live)
        top: Counter = Counter(f.rel.split("/", 1)[0] if "/" in f.rel else "(root)" for f in live)
        descriptors = [f.rel for f in live if f.kind == "build" or f.name.lower() in (
            "web.xml", "struts-config.xml", "faces-config.xml", "tiles-defs.xml", "application.xml",
            "ejb-jar.xml", "applicationcontext.xml", "beans.xml", "persistence.xml", "web.config",
            "global.asax", "app.config", "log4j.properties", "log4j.xml", "logback.xml")
            or re.search(r"(struts|tiles|spring|context|servlet|hibernate|validation)[\w.-]*\.xml$",
                         f.name.lower())]
        lines = [
            f"- Files analysed: {len(live)} (plus {len(self.files) - len(live)} generated/build-output files)",
            "- By kind: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()),
            "- Top extensions: " + ", ".join(f"{e} {v}" for e, v in exts.most_common(15)),
            "- Top-level directories: " + ", ".join(f"{d} ({v})" for d, v in top.most_common(20)),
            f"- Dominant text encoding: {self.dominant_encoding()}",
        ]
        if descriptors:
            lines.append("- Build files and descriptors: " + ", ".join(sorted(descriptors)[:50]))
        if self.build_output_dirs:
            lines.append("- Build output directories (ignored): " + ", ".join(sorted(self.build_output_dirs)))
        if self.javadoc_dirs:
            lines.append("- Generated documentation (ignored): " + ", ".join(sorted(self.javadoc_dirs)))
        if self.excluded_dirs:
            lines.append("- Excluded by the user (not analysed, still searched for references): "
                         + ", ".join(sorted(self.excluded_dirs)))
        if self.skipped:
            lines.append("- Not part of the analysis: " + ", ".join(f"{d} ({r})" for d, r in self.skipped[:20]))
        return "\n".join(lines)


class RefIndex:
    """Restricted inverted index: which referrer files mention each queried name.

    Only the queried tokens are stored, so memory stays proportional to the
    number of candidates instead of the size of the codebase. Matching is
    case-insensitive on purpose: it can only make things look *more* used.
    """

    def __init__(self, inv: Inventory, tokens: Set[str], fnames: Set[str]) -> None:
        self.inv = inv
        self.files: List[str] = []
        self.tokens: Dict[str, Set[int]] = defaultdict(set)
        self.fnames: Dict[str, Set[int]] = defaultdict(set)
        self.counts: Dict[Tuple[int, str], int] = {}
        tokens = {t.lower() for t in tokens}
        fnames = {f.lower() for f in fnames}
        for fi in inv.files:
            if not inv.is_referrer(fi):
                continue
            text = inv.text(fi)
            if text is None:
                continue
            fid = len(self.files)
            self.files.append(fi.rel)
            found = [t.lower() for t in TOKEN_RE.findall(text)]
            if fi.syntax == "cobol" or fi.ext == "jcl":
                found += [t.lower() for t in COBOL_TOKEN_RE.findall(text)]
            for tok in found:
                if tok in tokens:
                    self.tokens[tok].add(fid)
                    key = (fid, tok)
                    self.counts[key] = self.counts.get(key, 0) + 1
            if fnames:
                for fn in set(m.lower() for m in FNAME_RE.findall(text)):
                    if fn in fnames:
                        self.fnames[fn].add(fid)

    def files_with_token(self, token: str) -> Set[str]:
        return {self.files[i] for i in self.tokens.get(token.lower(), ())}

    def files_with_fname(self, fname: str) -> Set[str]:
        return {self.files[i] for i in self.fnames.get(fname.lower(), ())}

    def total_count(self, token: str) -> int:
        tok = token.lower()
        return sum(self.counts.get((fid, tok), 0) for fid in self.tokens.get(tok, ()))
