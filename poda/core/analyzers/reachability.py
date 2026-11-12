"""Unused code that is not commented out.

* Files: reachability from live roots (configuration, build files, entry points,
  tests, scripts, excluded directories) through name references. A candidate
  that no live file mentions is dead, including groups that only reference
  each other. Every file verdict is confirmed by the LLM before removal.
* Members: private methods/fields/procedures never used in their own file
  (deterministic, high confidence) and non-private ones whose name appears
  nowhere else in the project (LLM verification).
* Imports: Java single-type imports never used (deterministic) and Python
  imports never used (LLM verification: imports can have side effects).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ..inventory import FileInfo, Inventory, RefIndex, TOKEN_RE, is_vendor
from ...models import Finding, Findings
from ...utils.text import blank_c, split_lines, strip_eol
from . import ast_members
from .legacy_scanners import Member, extract_members_regex

ELIGIBLE_KINDS = {"source", "web", "resource"}
PUBLIC_PAGE_EXT = {"jsp", "jspx", "html", "htm", "xhtml", "shtml", "php", "phtml", "asp", "aspx",
                   "cshtml", "vbhtml"}
WEB_ENTRY_STEMS = {"index", "default", "favicon", "robots", "sitemap", "crossdomain", "humans"}

JAVA_ENTRY_ANN = re.compile(
    r"@(WebServlet|WebFilter|WebListener|Controller|RestController|Service|Component|Repository|"
    r"Configuration|SpringBootApplication|Entity|Embeddable|MappedSuperclass|Path|Provider|"
    r"ApplicationPath|Stateless|Stateful|Singleton|MessageDriven|Named|ManagedBean|FacesConverter|"
    r"FacesValidator|FacesComponent|WebService|WebServiceProvider|ServerEndpoint|Scheduled|Aspect|"
    r"ControllerAdvice|RestControllerAdvice|Mapper|Startup|Remote|Local|Interceptor|Decorator|"
    r"Converter|Mojo|Plugin|ApplicationScoped|RequestScoped|SessionScoped|Dependent|ApiController|"
    r"Route|HttpGet|HttpPost)\b")
MAIN_RE = re.compile(r"\bstatic\s+(?:public\s+)?void\s+main\s*\(|\bpublic\s+static\s+void\s+main\s*\(|"
                     r"\bstatic\s+(?:async\s+)?(?:void|int|Task)\s+Main\s*\(")
TEST_RE = re.compile(
    r"(^|/)(src/test|test|tests|__tests__|spec|testing)/|(Test|Tests|TestCase|IT|Spec)\."
    r"(java|cs|kt|scala|groovy|js|ts|vb)$|(^|/)test_[^/]*\.py$|_test\.(py|go|js|ts)$|"
    r"\.(spec|test)\.(js|ts|jsx|tsx)$")
PY_MAIN = re.compile(r"""if\s+__name__\s*==\s*['"]__main__['"]""")
PY_ENTRY = {"manage.py", "setup.py", "conftest.py", "__init__.py", "wsgi.py", "asgi.py", "__main__.py"}
TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|struct)\s+([A-Za-z_$][\w$]*)")
JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
VB_NAME = re.compile(r'Attribute\s+VB_Name\s*=\s*"([^"]+)"', re.I)
COBOL_PROGRAM = re.compile(r"PROGRAM-ID\.\s*['\"]?([A-Za-z0-9-]+)", re.I)
SERVLET_API = re.compile(r"\bimplements\b[^{]*\b(Filter|Servlet|ServletContextListener|HttpSessionListener)\b"
                         r"|\bextends\s+(HttpServlet|GenericServlet)\b")

CS_ENTRY_ATTR = re.compile(r"\[\s*(ApiController|Route|HttpGet|HttpPost|HttpPut|HttpDelete|WebMethod|"
                           r"WebService|ServiceContract|OperationContract|TestClass|TestMethod|Fact|"
                           r"Theory|TestFixture|Test)\b")
WEB_ROOT_NAMES = {"webcontent", "webroot", "webapp", "public", "www", "htdocs", "wwwroot",
                  "public_html", "web", "static", "site"}
REFLECTION = re.compile(r"getDeclaredMethod|getDeclaredField|getMethod\s*\(|getField\s*\(|Class\.forName|"
                        r"MethodUtils|PropertyUtils|BeanUtils|\.invoke\s*\(|GetType\(\)\.GetMethod|"
                        r"CallByName|getattr\s*\(|setattr\s*\(|__getattr__")
CALLBACKS = {n.lower() for n in """
main run call init destroy service doGet doPost doPut doDelete doHead doOptions doTrace doFilter
execute perform validate reset toString equals hashCode compareTo compare clone finalize close
iterator hasNext next remove get set put size isEmpty contains apply accept test getValue setValue
actionPerformed itemStateChanged valueChanged stateChanged propertyChange keyPressed keyReleased
keyTyped mouseClicked mousePressed mouseReleased mouseEntered mouseExited mouseMoved mouseDragged
focusGained focusLost windowClosing windowClosed windowOpened contextInitialized contextDestroyed
sessionCreated sessionDestroyed attributeAdded attributeRemoved attributeReplaced requestInitialized
requestDestroyed afterPropertiesSet setUp tearDown onMessage handle handleRequest process start stop
getServletInfo getServletConfig doStartTag doEndTag doAfterBody release setPageContext setParent
getParent onLoad onUnload onClick Page_Load Application_Start Session_Start Dispose Render OnInit
OnLoad getConnection getObjectInstance invoke postConstruct preDestroy update notify paint repaint
paintComponent""".split()}
SERIALIZATION = {"serialversionuid", "readobject", "writeobject", "readresolve", "writereplace",
                 "readobjectnodata", "serialpersistentfields"}
JAVA_IMPORT = re.compile(r"^[ \t]*import[ \t]+(static[ \t]+)?([\w$.]+?)(\.\*)?[ \t]*;[ \t]*$")
PY_IMPORT = re.compile(r"^import[ \t]+([\w.]+)(?:[ \t]+as[ \t]+(\w+))?[ \t]*$")
PY_FROM = re.compile(r"^from[ \t]+([\w.]+)[ \t]+import[ \t]+([\w, \t]+?)[ \t]*$")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def file_names(inv: Inventory, fi: FileInfo) -> Tuple[Set[str], Set[str], Optional[str]]:
    """Names by which other files can refer to `fi`: tokens, file names, FQCN."""
    tokens: Set[str] = set()
    fnames = {fi.name.lower()}
    fqcn = None
    # Images, fonts and stylesheets are always referenced with their extension;
    # matching their bare stem ("a", "logo", "menu") would make them look used.
    by_extension_only = fi.kind == "resource" or fi.ext in ("css", "scss", "less")
    if TOKEN_RE.fullmatch(fi.stem) and not by_extension_only:
        tokens.add(fi.stem.lower())
    text = inv.text(fi) if inv.is_text(fi) and fi.kind == "source" else None
    if text:
        if fi.ext in ("java", "cs", "kt", "scala", "groovy"):
            tokens.update(n.lower() for n in TYPE_DECL.findall(text)[:20])
            m = JAVA_PACKAGE.search(text)
            if m and fi.ext == "java":
                fqcn = f"{m.group(1)}.{fi.stem}"
        elif fi.ext in ("bas", "cls", "frm", "ctl", "dsr"):
            m = VB_NAME.search(text)
            if m:
                tokens.add(m.group(1).lower())
        elif fi.syntax == "cobol":
            if re.fullmatch(r"[A-Za-z0-9-]+", fi.stem):
                tokens.add(fi.stem.lower())
            m = COBOL_PROGRAM.search(text)
            if m:
                tokens.add(m.group(1).lower())
    return tokens, fnames, fqcn


def entry_reason(inv: Inventory, fi: FileInfo) -> Optional[str]:
    if TEST_RE.search(fi.rel):
        return "código de pruebas"
    if fi.kind in ("web", "resource", "source") and fi.stem.lower() in WEB_ENTRY_STEMS:
        parent = fi.parent.rsplit("/", 1)[-1].lower()
        if fi.parent == "" or parent in WEB_ROOT_NAMES:
            return "página o recurso de entrada en la raíz web"
    if fi.ext == "py":
        if fi.name in PY_ENTRY:
            return "punto de entrada de Python"
        if PY_MAIN.search(inv.text(fi) or ""):
            return "programa principal de Python"
    if fi.ext in ("java", "kt", "scala", "groovy", "cs"):
        if fi.name in ("package-info.java", "module-info.java"):
            return "descriptor de paquete o módulo"
        text = inv.text(fi) or ""
        if MAIN_RE.search(text):
            return "programa principal (main)"
        blanked = blank_c(text)
        if JAVA_ENTRY_ANN.search(blanked):
            return "anotación de framework (se descubre por escaneo)"
        if fi.ext == "cs" and (CS_ENTRY_ATTR.search(blanked) or fi.stem.endswith("Controller")):
            return "controlador o servicio .NET (se descubre por convención)"
    if fi.ext in ("dpr", "dpk", "lpr", "ashx", "asmx") or fi.name.lower() == "global.asax":
        return "punto de entrada del proyecto"
    return None



def extract_members(inv: Inventory, fi: FileInfo) -> List[Member]:
    """Members of a file: tree-sitter when there is a grammar, v1 scanners otherwise."""
    found = ast_members.extract(inv, fi)
    return found if found is not None else extract_members_regex(inv, fi)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _count_word(text: str, name: str, ignore_case: bool = False) -> int:
    flags = re.I if ignore_case else 0
    return len(re.findall(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text, flags))


def detect_unused_code(inv: Inventory, findings: Findings, jar_classes: Optional[Dict[str, str]] = None) -> RefIndex:
    """Files, members and imports. Returns the reference index for reuse."""
    jar_classes = jar_classes or {}
    eligible = [fi for fi in inv.files if fi.kind in ELIGIBLE_KINDS and not fi.excluded
                and not fi.generated and fi.rel not in inv.junk]
    entries: Dict[str, str] = {}
    names: Dict[str, Tuple[Set[str], Set[str], Optional[str]]] = {}
    query_tokens: Set[str] = set()
    query_fnames: Set[str] = set()
    for fi in eligible:
        reason = entry_reason(inv, fi)
        if reason:
            entries[fi.rel] = reason
            continue
        names[fi.rel] = file_names(inv, fi)
        query_tokens |= names[fi.rel][0]
        query_fnames |= names[fi.rel][1]

    members: Dict[str, List[Member]] = {}
    for fi in inv.files:
        if fi.kind != "source" or fi.excluded or fi.generated or fi.rel in inv.junk or is_vendor(fi.rel):
            continue
        found = extract_members(inv, fi)
        if found:
            members[fi.rel] = found
            for mb in found:
                query_tokens.add(mb.name.lower())
                prop = _bean_property(mb.name)
                if prop:
                    query_tokens.add(prop.lower())

    index = RefIndex(inv, query_tokens, query_fnames)

    # -- files: reachability ------------------------------------------------
    refs_of: Dict[str, Set[str]] = {}
    for rel, (toks, fns, _fq) in names.items():
        referrers: Set[str] = set()
        for tok in toks:
            referrers |= index.files_with_token(tok)
        for fn in fns:
            referrers |= index.files_with_fname(fn)
        referrers.discard(rel)
        refs_of[rel] = referrers
    out_edges: Dict[str, Set[str]] = defaultdict(set)
    for cand, referrers in refs_of.items():
        for ref in referrers:
            out_edges[ref].add(cand)
    candidates = set(refs_of)
    live = {rel for rel in index.files if rel not in candidates}
    stack = list(live)
    while stack:
        current = stack.pop()
        for cand in out_edges.get(current, ()):
            if cand not in live:
                live.add(cand)
                stack.append(cand)
    dead = sorted(candidates - live)
    dead_set = set(dead)

    collapsed = _collapse_dirs(inv, dead_set)
    in_collapsed = {f.rel for d in collapsed for f in inv.under(d)}
    for rel_dir in sorted(collapsed):
        files_under = inv.under(rel_dir)
        dead_here = [f.rel for f in files_under if f.rel in dead_set]
        f = Finding(category="unused_directory", subtype="unreferenced_directory", path=rel_dir,
                    reason=f"directorio sin uso: ninguno de sus {len(files_under)} ficheros se referencia desde código vivo",
                    confidence="medium", needs_llm=True, detectors=["references"],
                    evidence=[f"{len(dead_here)} ficheros sin referencias vivas, p. ej.: "
                              + ", ".join(sorted(dead_here)[:5])],
                    edit={"op": "delete_path"})
        f.removed_files = len(files_under)
        findings.add(f)
    for rel in dead:
        if rel in in_collapsed:
            continue
        fi = inv.by_rel[rel]
        toks, fns, fqcn = names[rel]
        searched = sorted(toks | fns)
        evidence = [f"Nombres buscados sin referencias desde ficheros vivos: {', '.join(searched[:6])}"]
        referrers = sorted(refs_of[rel])
        subtype = "unreferenced"
        confidence = "medium"
        if referrers:
            subtype = "only_referenced_by_dead_code"
            evidence.append("Solo lo mencionan ficheros también sin uso: " + ", ".join(referrers[:5]))
        if fi.ext in PUBLIC_PAGE_EXT and "/WEB-INF/" not in f"/{rel}" and "/META-INF/" not in f"/{rel}":
            confidence = "low"
            evidence.append("Página accesible por URL directa (no está bajo WEB-INF)")
        if fqcn and fqcn in jar_classes:
            confidence = "low"
            evidence.append(f"Tiene el mismo nombre de clase completo que una clase de {jar_classes[fqcn]}: "
                            "puede sombrear a la clase de la librería en tiempo de ejecución")
        if fi.syntax == "cobol":
            confidence = "low"
            evidence.append("Programa COBOL: puede invocarse desde JCL, CICS o planificadores externos")
        if fi.ext == "java" and SERVLET_API.search(inv.text(fi) or ""):
            evidence.append("Implementa una interfaz de servlet/filtro/listener: solo se ejecuta si está "
                            "declarado en web.xml o anotado")
        findings.add(Finding(
            category="unused_file", subtype=subtype, path=rel,
            reason="fichero sin referencias desde código vivo" if not referrers else
                   "fichero que solo usa código que también está muerto",
            confidence=confidence, needs_llm=True, detectors=["references"], evidence=evidence,
            edit={"op": "delete_path"}))

    # -- members ------------------------------------------------------------
    for rel, found in members.items():
        if rel in dead_set or rel in in_collapsed:
            continue
        fi = inv.by_rel[rel]
        text = inv.text(fi) or ""
        lines = split_lines(text)
        reflective = bool(REFLECTION.search(text))
        ignore_case = fi.ext in ("bas", "cls", "frm", "ctl", "dsr", "vb")
        for mb in found:
            if mb.skip:
                continue
            low = mb.name.lower()
            if low in CALLBACKS or low in SERIALIZATION or low.startswith("test"):
                continue
            in_file = _count_word(text, mb.name, ignore_case)
            if in_file != 1:
                continue
            others = index.files_with_token(mb.name) - {rel}
            prop = _bean_property(mb.name)
            if prop and (index.files_with_token(prop) - {rel} or _count_word(text, prop, ignore_case)):
                continue
            if mb.private:
                if mb.lang == "py" and others:
                    continue
                confidence, needs_llm = ("medium", True) if reflective else ("high", False)
                subtype = f"private_{mb.kind}"
                reason = f"{_kind_es(mb.kind)} privado que no se usa en su fichero"
            else:
                if others or mb.in_type_with_supertype and mb.kind == "method":
                    continue
                confidence, needs_llm = "medium", True
                subtype = f"unreferenced_{mb.kind}"
                reason = f"{_kind_es(mb.kind)} que no se usa en ningún sitio del proyecto"
            snippet = "\n".join(strip_eol(x) for x in lines[mb.start_line - 1:min(mb.end_line, mb.start_line + 9)])
            evidence = [f"'{mb.name}' solo aparece en su propia declaración"]
            if reflective:
                evidence.append("El fichero usa reflexión: hay que confirmar que no se invoca por nombre")
            findings.add(Finding(
                category="unused_member", subtype=subtype, path=rel, symbol=mb.name,
                start_line=mb.start_line, end_line=mb.end_line, reason=reason,
                confidence=confidence, needs_llm=needs_llm, detectors=["references"],
                evidence=evidence, snippet=snippet,
                edit={"op": "delete_lines", "ranges": [[mb.start_line, mb.end_line]]}))

    # -- imports ------------------------------------------------------------
    for fi in inv.files:
        if fi.excluded or fi.generated or fi.rel in inv.junk or fi.rel in dead_set or fi.rel in in_collapsed \
                or is_vendor(fi.rel):
            continue
        if fi.ext == "java":
            _java_imports(inv, fi, findings)
        elif fi.ext == "py" and fi.name != "__init__.py":
            _python_imports(inv, fi, findings)
    return index


def _kind_es(kind: str) -> str:
    return {"method": "método", "field": "campo", "function": "función",
            "procedure": "procedimiento"}.get(kind, kind)


def _bean_property(name: str) -> Optional[str]:
    m = re.match(r"^(get|set|is)([A-Z]\w*)$", name)
    if not m:
        return None
    prop = m.group(2)
    return prop[0].lower() + prop[1:]


def _collapse_dirs(inv: Inventory, dead: Set[str]) -> Set[str]:
    per_dir: Dict[str, List[FileInfo]] = defaultdict(list)
    for fi in inv.files:
        parts = fi.rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            per_dir["/".join(parts[:i])].append(fi)
    collapsible: Set[str] = set()
    for rel_dir, files in per_dir.items():
        if rel_dir in inv.build_output_dirs or rel_dir in inv.excluded_dirs:
            continue
        dead_count = sum(1 for f in files if f.rel in dead)
        if dead_count < 2:
            continue
        if all(f.rel in dead or f.kind == "doc" or f.rel in inv.junk for f in files):
            collapsible.add(rel_dir)
    return {d for d in collapsible if (d.rsplit("/", 1)[0] if "/" in d else "") not in collapsible}


def _java_imports(inv: Inventory, fi: FileInfo, findings: Findings) -> None:
    text = inv.text(fi)
    if not text:
        return
    lines = split_lines(text)
    import_lines = {}
    for idx, raw in enumerate(lines, start=1):
        m = JAVA_IMPORT.match(strip_eol(raw))
        if m:
            import_lines[idx] = m
    if not import_lines:
        return
    body = "".join(raw for idx, raw in enumerate(lines, start=1) if idx not in import_lines)
    seen: Set[str] = set()
    for idx, m in import_lines.items():
        target = m.group(2)
        if m.group(3):
            continue  # wildcard import
        key = (m.group(1) or "") + target
        simple = target.rsplit(".", 1)[-1]
        duplicate = key in seen
        seen.add(key)
        if not duplicate and _count_word(body, simple):
            continue
        findings.add(Finding(
            category="unused_import", subtype="duplicate_import" if duplicate else "unused_import",
            path=fi.rel, start_line=idx, end_line=idx, symbol=target,
            reason="import duplicado" if duplicate else f"import de {simple} que no se usa",
            confidence="high", needs_llm=False, detectors=["imports"],
            evidence=[f"'{simple}' no aparece en el resto del fichero"] if not duplicate else [],
            snippet=strip_eol(lines[idx - 1]),
            edit={"op": "delete_lines", "ranges": [[idx, idx]]}))


def _python_imports(inv: Inventory, fi: FileInfo, findings: Findings) -> None:
    text = inv.text(fi)
    if not text or "__all__" in text:
        return
    lines = split_lines(text)
    for idx, raw in enumerate(lines, start=1):
        line = strip_eol(raw)
        used_names: List[str] = []
        m = PY_IMPORT.match(line)
        if m:
            used_names = [m.group(2) or m.group(1).split(".")[0]]
        else:
            m = PY_FROM.match(line)
            if not m or m.group(1) == "__future__":
                continue
            for part in m.group(2).split(","):
                bits = part.split()
                if not bits:
                    continue
                used_names.append(bits[-1] if len(bits) == 3 and bits[1] == "as" else bits[0])
        if not used_names:
            continue
        rest = "".join(r for j, r in enumerate(lines, start=1) if j != idx)
        if any(_count_word(rest, name) for name in used_names):
            continue
        findings.add(Finding(
            category="unused_import", subtype="unused_import", path=fi.rel, start_line=idx,
            end_line=idx, symbol=", ".join(used_names),
            reason="import de Python que no se usa en el fichero",
            confidence="medium", needs_llm=True, detectors=["imports"],
            evidence=[f"{', '.join(used_names)} no aparece en el resto del fichero"],
            snippet=line, edit={"op": "delete_lines", "ranges": [[idx, idx]]}))
