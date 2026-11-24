/* EN (source) -> ES translation for the landing page. Same idea as the dashboard's i18n.js:
   exact dictionary on trimmed text nodes + a few regex rules, and a MutationObserver for text the demo writes. */
(() => {
  'use strict';
  const KEY = 'poda-site-lang';
  const ES = {
    // nav + chrome
    'Demo': 'Demo', 'How it works': 'Cómo funciona', 'Dashboard': 'Dashboard', 'Providers': 'Proveedores', 'Safety': 'Seguridad', 'Get started': 'Empezar',
    'Toggle theme': 'Cambiar tema', 'Sections': 'Secciones', 'Animated example of a cleanup': 'Ejemplo animado de una limpieza',
    'Pipeline phases': 'Fases del proceso', 'Cleanup counters': 'Contadores de la limpieza', 'Key facts': 'Datos clave', 'Steps': 'Pasos',
    'Simulated run log': 'Log de ejecución simulado',
    // hero
    'Dead code cleanup': 'Limpieza de código muerto',
    'Prune the dead code.': 'Poda el código muerto.', 'Keep the application.': 'Conserva la aplicación.',
    'Poda AI removes what nothing uses from a legacy application before you document or migrate it. SonarQube and a static analysis find it, an AI confirms it, and a deterministic engine removes it from a copy. Your original is never touched.':
      'Poda AI elimina lo que nadie usa de una aplicación legacy antes de documentarla o migrarla. SonarQube y un análisis estático lo encuentran, una IA lo confirma y un motor determinista lo elimina en una copia. Tu original nunca se toca.',
    'Watch it work': 'Míralo en marcha',
    // demo
    'See it in action': 'Míralo en acción', 'From legacy to lean, line by line': 'De legacy a ligero, línea a línea',
    'A simulated run on a tiny sample. The real thing runs the same eight phases on your whole application.':
      'Una ejecución simulada sobre un ejemplo mínimo. La real pasa por las mismas ocho fases sobre toda tu aplicación.',
    'Replay': 'Repetir',
    'Inventory': 'Inventario', 'Static analysis': 'Análisis estático', 'Context (AI)': 'Contexto (IA)', 'Verification (AI)': 'Verificación (IA)',
    'Discovery (AI)': 'Descubrimiento (IA)', 'Apply': 'Aplicar', 'Reports': 'Informes',
    'findings': 'hallazgos', 'AI verdicts': 'veredictos de la IA', 'lines removed': 'líneas eliminadas', 'left for review': 'pendientes de revisión',
    'Press Replay to run it again.': 'Pulsa Repetir para ejecutarlo otra vez.', 'Running the cleanup…': 'Ejecutando la limpieza…',
    'remove · high': 'eliminar · alta', 'review': 'revisar',
    // facts
    'pipeline phases': 'fases del proceso', 'edits made by the AI itself': 'ediciones hechas por la propia IA',
    'built-in AI providers': 'proveedores de IA integrados', 'free and open source': 'gratis y de código abierto',
    // how
    'Eight phases, always in this order': 'Ocho fases, siempre en este orden',
    'Nothing is removed without the AI: not even what SonarQube or the static analysis consider safe.':
      'No se elimina nada sin la IA: ni siquiera lo que SonarQube o el análisis estático consideran seguro.',
    'Classifies every file, detects its encoding and counts the real lines of code.': 'Clasifica cada archivo, detecta su codificación y cuenta las líneas de código reales.',
    'Analyses the whole app with only the unused-code rules, in its own isolated instance.': 'Analiza toda la app solo con las reglas de código sin uso, en su propia instancia aislada.',
    'Finds what SonarQube cannot: unreachable files, unused libraries, dead members, junk.': 'Encuentra lo que SonarQube no ve: archivos inalcanzables, librerías sin uso, miembros muertos y basura.',
    'An agent learns how the app starts and what must never be deleted.': 'Un agente aprende cómo arranca la app y qué no se debe borrar nunca.',
    'Every finding gets a verdict: remove, keep or review, with a confidence and a reason.': 'Cada hallazgo recibe un veredicto: eliminar, conservar o revisar, con su confianza y su motivo.',
    'Looks for what no analysis sees: forever-off flags, dead config, test pages.': 'Busca lo que ningún análisis ve: flags apagados para siempre, configuración muerta y páginas de prueba.',
    'Removes only high-confidence verdicts from a copy, byte for byte. A file that gets worse is reverted.': 'Elimina solo los veredictos de confianza alta en una copia, byte a byte. Un archivo que empeora se revierte.',
    'Report, metrics, findings and the full log. Tokens, cost and lines before and after.': 'Informe, métricas, hallazgos y el log completo. Tokens, coste y líneas antes y después.',
    // dashboard mock
    'The dashboard': 'El dashboard', 'Three steps, one local page': 'Tres pasos, una página local',
    'Connect a provider, pick the model, clean an application. Try the simulated dashboard below.': 'Conecta un proveedor, elige el modelo y limpia una aplicación. Prueba el dashboard simulado de abajo.',
    'Connect at least one': 'Conecta al menos uno', 'Model': 'Modelo', 'Pick the model': 'Elige el modelo', 'Cleanup': 'Limpieza', 'Pick the app and launch': 'Elige la app y lanza',
    'LiteLLM gateway': 'Gateway LiteLLM', '✔ Connected': '✔ Conectado', 'Gateway URL': 'URL del gateway', 'API key': 'Clave API',
    '✔ Connected as nalmale': '✔ Conectado como nalmale', 'Reconnect with GitHub': 'Reconectar con GitHub',
    '✔ Connected as you@email.com': '✔ Conectado como you@email.com',
    'Your Pro/Max plan through the official CLI. Poda AI never sees your token.': 'Tu plan Pro/Max a través del CLI oficial. Poda AI nunca ve tu token.',
    'Disconnect': 'Desconectar',
    'Main model': 'Modelo principal', 'Context, verification with tools and discovery.': 'Contexto, verificación con herramientas y descubrimiento.',
    'Triage model': 'Modelo de triaje', 'First review of each finding, without tools.': 'Primera revisión de cada hallazgo, sin herramientas.',
    '✔ Changes are saved automatically.': '✔ Los cambios se guardan automáticamente.',
    'Not cleaned yet': 'Aún sin limpiar', 'Last: 3 files · 120 lines': 'Última: 3 archivos · 120 líneas',
    'Start cleanup': 'Iniciar limpieza', 'Ready': 'Listo', 'Running': 'En marcha', 'Finished': 'Terminada', 'Run again': 'Ejecutar de nuevo',
    'Inventory: 412 files classified, 38,204 lines of code': 'Inventario: 412 archivos clasificados, 38.204 líneas de código',
    'SonarQube: unused-code rules → 1,096 issues': 'SonarQube: reglas de código sin uso → 1.096 incidencias',
    'Static analysis: 214 more candidates, merged with SonarQube': 'Análisis estático: 214 candidatos más, fusionados con SonarQube',
    'Context: entry points and protected files identified': 'Contexto: puntos de entrada y archivos protegidos identificados',
    'Verification: 1,310 findings judged\n  ▲ 87 left for manual review (medium confidence)': 'Verificación: 1.310 hallazgos juzgados\n  ▲ 87 pendientes de revisión manual (confianza media)',
    'Discovery: 6 forever-off flags, 2 dead mappings': 'Descubrimiento: 6 flags apagados para siempre, 2 mapeos muertos',
    'Apply: 91 files deleted, 2,847 lines removed from the copy': 'Aplicar: 91 archivos borrados, 2.847 líneas eliminadas de la copia',
    'Reports: poda_report.md, findings.json, execution_metrics.md': 'Informes: poda_report.md, findings.json, execution_metrics.md',
    // removes
    'What it removes': 'Qué elimina', 'Everything the AI can prove is unused': 'Todo lo que la IA puede demostrar que no se usa',
    'Commented-out code': 'Código comentado',
    'Blocks of code left in comments. Explanatory comments stay: they are useful documentation.': 'Bloques de código dejados en comentarios. Los comentarios explicativos se quedan: son documentación útil.',
    'Unused imports and members': 'Imports y miembros sin uso',
    'Imports, private and public methods, functions and fields that nothing reaches.': 'Imports, métodos privados y públicos, funciones y campos que nada alcanza.',
    'Unreachable code': 'Código inalcanzable',
    ', constant-false flags, statements after': ', flags constantes en falso, sentencias tras', 'or': 'o',
    'Dead files and folders': 'Archivos y carpetas muertos',
    'Classes, JSPs, scripts, images and vendor code that no live code reaches.': 'Clases, JSPs, scripts, imágenes y código de terceros que ningún código vivo alcanza.',
    'Unused libraries': 'Librerías sin uso',
    'Jars and declared dependencies in Maven, Gradle, npm, pip, VB and NuGet that nothing uses.': 'Jars y dependencias declaradas en Maven, Gradle, npm, pip, VB y NuGet que nada usa.',
    'Dead logic and config': 'Lógica y configuración muertas',
    'Forever-off flags, mappings to classes that no longer exist, test pages and old versions.': 'Flags apagados para siempre, mapeos a clases que ya no existen, páginas de prueba y versiones antiguas.',
    // providers
    'Bring your own AI': 'Trae tu propia IA', 'Use the provider you already pay for': 'Usa el proveedor que ya pagas',
    'Point it at your company gateway with a URL and an API key. Any model behind it works.': 'Apúntalo al gateway de tu empresa con una URL y una clave API. Vale cualquier modelo que haya detrás.',
    'Connect with a one-time code. Costs are estimated from GitHub\'s published per-model rates.': 'Conecta con un código de un solo uso. Los costes se estiman con las tarifas por modelo que publica GitHub.',
    'Sign in with your Pro/Max account through the official': 'Inicia sesión con tu cuenta Pro/Max mediante el comando oficial',
    'CLI. Poda AI keeps its own session and never sees your token.': '. Poda AI mantiene su propia sesión y nunca ve tu token.',
    'Also works with any LiteLLM provider from the command line: Bedrock, OpenAI, Azure, Gemini…': 'También funciona con cualquier proveedor de LiteLLM desde la línea de comandos: Bedrock, OpenAI, Azure, Gemini…',
    // safety
    'Safety first': 'La seguridad primero', 'Built so you can trust the result': 'Hecho para que puedas fiarte del resultado',
    'The original is never modified.': 'El original nunca se modifica.', 'Every cleanup writes a copy under': 'Cada limpieza escribe una copia en',
    ', apart from your application.': ', aparte de tu aplicación.',
    'The AI never edits code.': 'La IA nunca edita código.',
    'It only returns verdicts. Poda AI applies the deletions deterministically, byte for byte, and checks the exact lines first.': 'Solo devuelve veredictos. Poda AI aplica los borrados de forma determinista, byte a byte, y comprueba antes las líneas exactas.',
    'Only high confidence is removed.': 'Solo se elimina con confianza alta.', 'Anything uncertain goes to the report for manual review.': 'Todo lo dudoso va al informe para revisión manual.',
    'Worse files are reverted.': 'Los archivos que empeoran se revierten.', 'If an edited file stops being valid, it is restored automatically.': 'Si un archivo editado deja de ser válido, se restaura automáticamente.',
    // start
    'Up and running in three commands': 'En marcha en tres comandos', 'You need Docker. Copy each legacy app into': 'Necesitas Docker. Copia cada app legacy en', ', then:': ', y luego:',
    'Copy': 'Copiar', 'Copied': 'Copiado', 'Press Ctrl+C': 'Pulsa Ctrl+C',
    '# 1 · clone and start Poda AI with its own SonarQube': '# 1 · clona y arranca Poda AI con su propio SonarQube',
    '# 2 · open the dashboard at http://127.0.0.1:8770/': '# 2 · abre el dashboard en http://127.0.0.1:8770/',
    'Read the full guide': 'Leer la guía completa', 'Star on GitHub': 'Dale una estrella en GitHub',
    'by': 'por', 'Issues': 'Incidencias', 'License': 'Licencia',
    // head
    'Poda AI · Dead code cleanup for legacy applications': 'Poda AI · Limpieza de código muerto para aplicaciones legacy',
    'Poda AI removes dead code from legacy applications with SonarQube, static analysis and an AI that only gives verdicts. Your original is never touched.':
      'Poda AI elimina el código muerto de aplicaciones legacy con SonarQube, análisis estático y una IA que solo da veredictos. Tu original nunca se toca.',
    'SonarQube + static analysis + AI verdicts. A clean copy, a report and the metrics. The original is never touched.':
      'SonarQube + análisis estático + veredictos de la IA. Una copia limpia, un informe y las métricas. El original nunca se toca.',
  };
  const RULES = [
    [/^Done: (\d+) lines removed from the copy, 1 left for review\. The original was never touched\.$/,
      'Listo: $1 líneas eliminadas de la copia, 1 pendiente de revisión. El original no se ha tocado.'],
  ];
  const ATTRS = ['title', 'aria-label', 'content'];
  const orig = new WeakMap();      // text node -> English text
  const origAttr = new WeakMap();  // element -> {attr: English}
  let lang = 'en';

  function translate(src) {
    const core = src.trim();
    if (!core) return src;
    const lead = src.slice(0, src.indexOf(core)), trail = src.slice(src.indexOf(core) + core.length);
    let out = Object.prototype.hasOwnProperty.call(ES, core) ? ES[core] : null;
    if (out === null) for (const [re, rep] of RULES) if (re.test(core)) { out = core.replace(re, rep); break; }
    if (out === null) return src;
    return (/^[.,;:]/.test(out) ? '' : lead) + out + trail;
  }
  const skip = (n) => { const p = n.parentElement; return !p || p.closest('script,style,#code'); };

  function text(node) {
    if (skip(node)) return;
    let o = orig.get(node);
    if (o === undefined) { o = node.nodeValue; orig.set(node, o); }
    const next = lang === 'es' ? translate(o) : o;
    if (node.nodeValue !== next) node.nodeValue = next;
  }
  function walk(root) {
    if (root.nodeType === 3) return text(root);
    if (root.nodeType !== 1) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) text(n);
  }
  function attrs() {
    document.querySelectorAll('[title],[aria-label],meta[name="description"],meta[property^="og:"]').forEach(el => {
      const rec = origAttr.get(el) || {};
      origAttr.set(el, rec);
      for (const a of ATTRS) {
        if (!el.hasAttribute(a)) continue;
        if (!(a in rec)) rec[a] = el.getAttribute(a);
        el.setAttribute(a, lang === 'es' ? translate(rec[a]) : rec[a]);
      }
    });
  }
  const titleEN = document.title;
  function apply() {
    document.documentElement.lang = lang === 'es' ? 'es' : 'en-GB';
    document.title = lang === 'es' ? translate(titleEN) : titleEN;
    walk(document.body);
    attrs();
    document.querySelectorAll('[data-href-es]').forEach(a => {
      if (!a.dataset.hrefEn) a.dataset.hrefEn = a.getAttribute('href');
      a.setAttribute('href', lang === 'es' ? a.dataset.hrefEs : a.dataset.hrefEn);
    });
    document.querySelectorAll('.lang button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.lang === lang)));
    document.dispatchEvent(new CustomEvent('sitelang', { detail: lang }));
  }

  new MutationObserver((muts) => muts.forEach(m => m.addedNodes.forEach(n => walk(n)))).observe(document.body, { childList: true, subtree: true });

  try { lang = localStorage.getItem(KEY) === 'es' ? 'es' : 'en'; } catch { /* storage blocked */ }
  const q = new URLSearchParams(location.search).get('lang');   // ?lang=es shares a Spanish link
  if (q === 'es' || q === 'en') lang = q;
  document.querySelectorAll('.lang button').forEach(b => b.addEventListener('click', () => {
    lang = b.dataset.lang === 'es' ? 'es' : 'en';
    try { localStorage.setItem(KEY, lang); } catch { /* only this visit */ }
    apply();
  }));
  window.PodaI18n = { t: (s) => (lang === 'es' ? translate(s) : s), get lang() { return lang; } };
  apply();
})();
