/* Poda AI · idioma de la interfaz. El texto fuente de la página está en español; el inglés (Reino Unido, por
   defecto) se aplica al vuelo: diccionario exacto + patrones para los textos con datos. Los textos nuevos que no
   estén aquí se quedan en español, nunca se rompen. */
(() => {
  'use strict';
  const EN = {
    'Poda AI': 'Poda AI', 'por': 'by', 'Modo oscuro': 'Dark mode', 'Modo claro': 'Light mode',
    'Cambiar a modo claro': 'Switch to light mode', 'Cambiar a modo oscuro': 'Switch to dark mode',
    'Limpieza de código muerto': 'Dead code cleanup',
    'Conecta un proveedor de IA, elige el modelo y limpia tus aplicaciones legacy.': 'Connect an AI provider, pick the model and clean your legacy applications.',
    'Proveedores': 'Providers', 'Conecta al menos uno': 'Connect at least one', 'Modelo': 'Model', 'Elige el modelo': 'Pick the model',
    'Limpieza': 'Cleanup', 'Elige la aplicación y lánzala': 'Pick the application and launch it', 'Pasos': 'Steps',
    'Paso 1': 'Step 1', 'Paso 2': 'Step 2', 'Paso 3': 'Step 3', 'Proveedores de IA': 'AI providers',
    'Añade los proveedores que uses; después elige uno de ellos y un modelo que ofrezca. Las credenciales se guardan en este equipo, no se vuelven a mostrar y mandan sobre .env.': 'Add the providers you use, then pick one of them and a model it offers. Credentials are stored on this computer, are never shown again and take precedence over .env.',
    'Hay una limpieza en marcha.': 'A cleanup is running.',
    'Mientras dura, los proveedores no se pueden guardar, quitar, desconectar ni actualizar. El modelo sí se puede cambiar en el paso 2: se aplicará a la próxima limpieza.': 'While it runs, providers cannot be saved, removed, disconnected or refreshed. The model can still be changed in step 2: it will apply to the next cleanup.',
    'Para empezar, configura al menos un proveedor:': 'To get started, set up at least one provider:',
    'LiteLLM (URL y API key), GitHub Copilot o Claude (con tu plan Pro/Max). Después elige el modelo más abajo. No hace falta tocar .env.': 'LiteLLM (URL and API key), GitHub Copilot or Claude (with your Pro/Max plan). Then pick the model below. You do not need to touch .env.',
    'LiteLLM gateway': 'LiteLLM gateway', 'Comprobando…': 'Checking…', 'URL del gateway (obligatoria)': 'Gateway URL (required)',
    'API key (obligatoria)': 'API key (required)', 'Guardar': 'Save', 'Actualizar modelos': 'Refresh models', 'Quitar LiteLLM': 'Remove LiteLLM',
    'GitHub Copilot': 'GitHub Copilot', 'Token de acceso': 'Access token', 'Conectar con GitHub': 'Connect with GitHub',
    'Reconectar con GitHub': 'Reconnect with GitHub', 'Guardar token': 'Save token', 'Desconectar': 'Disconnect', 'Claude': 'Claude',
    'Usa tu plan Pro/Max con el CLI oficial': 'Use your Pro/Max plan with the official',
    '. La sesión es propia de Poda AI (independiente de la de tu equipo): inicias sesión con Google o e-mail en la web de Claude y pegas aquí el código. Poda AI nunca ve tu token. Cuenta contra el límite de uso de tu plan y solo verifica fragmentos: no usa el agente.': ' CLI. The session belongs to Poda AI (independent of your computer\'s): you sign in with Google or e-mail on the Claude website and paste the code here. Poda AI never sees your token. It counts against your plan\'s usage limit and only verifies snippets: it does not use the agent.',
    'Iniciar sesión con Claude': 'Sign in with Claude', 'Cambiar de cuenta': 'Switch account',
    'Siguiente: elegir el modelo →': 'Next: pick the model →', 'Modelo de IA': 'AI model',
    'El modelo principal construye el contexto del proyecto, hace de agente y descubre código muerto; el de triaje (opcional, más barato) valida los hallazgos con su evidencia. Los cambios se aplican en la próxima limpieza.': 'The main model builds the project context, acts as the agent and discovers dead code; the triage model (optional, cheaper) validates the findings with their evidence. Changes apply to the next cleanup.',
    'Modelo principal': 'Main model', 'Agente': 'Agent', 'Contexto, verificación con herramientas y descubrimiento.': 'Context, verification with tools and discovery.',
    'Proveedor': 'Provider', 'Modelo de triaje': 'Triage model', 'Snippet': 'Snippet', 'Primera revisión de cada hallazgo, sin herramientas.': 'First review of each finding, without tools.',
    'Comprobando la configuración…': 'Checking the configuration…',
    'Hay una limpieza en marcha: sigue con el modelo con el que empezó. Lo que cambies aquí se aplicará a la próxima.': 'A cleanup is running: it keeps the model it started with. Whatever you change here applies to the next one.',
    'Los cambios se guardan automáticamente.': 'Changes are saved automatically.', 'Volver a la configuración de .env': 'Go back to the .env configuration',
    '← Proveedores': '← Providers', 'Siguiente: limpiar una aplicación →': 'Next: clean an application →', 'Limpiar una aplicación': 'Clean an application',
    'Elige una aplicación de la carpeta de trabajo y lanza la limpieza. El original no se toca: Poda AI deja la copia limpia, el informe y las métricas aparte, en': 'Pick an application from the working folder and launch the cleanup. The original is never touched: Poda AI leaves the clean copy, the report and the metrics separately, in',
    'output/<aplicación>': 'output/<application>', 'Aplicaciones en': 'Applications in', 'Actualizar lista': 'Refresh list', 'SonarQube': 'SonarQube',
    'Primera fase de cada limpieza: detecta y resume; después la IA valida todo. Nada se elimina sin la IA.': 'First phase of every cleanup: it detects and summarises; then the AI validates everything. Nothing is removed without the AI.',
    'Cargando…': 'Loading…',
    'La limpieza es siempre completa: SonarQube, análisis de Poda AI, validación y descubrimiento con IA, y copia limpia. Solo se elimina lo que la IA confirma con confianza alta; el resto queda en el informe.': 'The cleanup is always complete: SonarQube, Poda AI analysis, AI validation and discovery, and a clean copy. Only what the AI confirms with high confidence is removed; the rest stays in the report.',
    'Esta aplicación ya tiene una limpieza anterior:': 'This application already has a previous cleanup:',
    'Reutilizar las respuestas de la IA (más rápido y barato)': 'Reuse the AI answers (faster and cheaper)', 'Empezar de cero': 'Start from scratch',
    'Iniciar limpieza': 'Start cleanup', 'Detener': 'Stop', '← Modelo': '← Model', 'Ejecución': 'Run', 'Registro': 'Log', 'Resultado': 'Result',
    'Informe': 'Report', 'Métricas y coste': 'Metrics and cost', 'Registro completo': 'Full log',
    'Poda AI · por nalmale': 'Poda AI · by nalmale', 'Poda AI · por': 'Poda AI · by',
    'Aplicación a limpiar': 'Application to clean', 'Registro de la ejecución': 'Run log',
    'Token OAuth (gho_…), o usa Conectar con GitHub': 'OAuth token (gho_…), or use Connect with GitHub',
    '— elige un proveedor —': '— pick a provider —', '— elige un modelo —': '— pick a model —', '— el mismo que el principal —': '— same as the main one —',
    '(conecta un proveedor)': '(connect a provider)', '(sin modelos: pulsa Actualizar modelos)': '(no models: press Refresh models)', '(elige primero un proveedor)': '(pick a provider first)',
    'guardado aquí': 'saved here', 'desde .env': 'from .env', 'vía CLI de Claude': 'via the Claude CLI', 'sesión de poda login': 'poda login session',
    'Listo': 'Ready', 'Sin configurar': 'Not set up', ' (en caché)': ' (cached)', ' · desde .env': ' · from .env', ' · guardado aquí': ' · saved here',
    ' · vía CLI de Claude': ' · via the Claude CLI', ' · sesión de poda login': ' · poda login session',
    'elegido aquí': 'chosen here', 'según .env': 'from .env', 'el proveedor': 'the provider', 'el modelo principal': 'the main model',
    'el modelo de triaje': 'the triage model', 'el modelo': 'the model', 'el gateway': 'the gateway', 'el token': 'the token', 'la vuelta a .env': 'switching back to .env',
    'El proveedor guardado.': 'Provider saved.', 'El modelo principal guardado.': 'Main model saved.', 'El modelo de triaje guardado.': 'Triage model saved.', 'La vuelta a .env guardado.': 'Switch back to .env saved.',
    'La API key es obligatoria: pégala para conectar LiteLLM.': 'The API key is required: paste it to connect LiteLLM.',
    'Guardando el gateway LiteLLM…': 'Saving the LiteLLM gateway…', 'LiteLLM quitado.': 'LiteLLM removed.', 'Claude desconectado.': 'Claude disconnected.',
    'GitHub Copilot desconectado.': 'GitHub Copilot disconnected.', 'Pega primero un token.': 'Paste a token first.',
    'Guardando el token de GitHub Copilot…': 'Saving the GitHub Copilot token…', 'Código copiado.': 'Code copied.', 'Copiar': 'Copy', 'Cerrar': 'Close', 'Cancelar': 'Cancel',
    'Empezando…': 'Starting…', 'Esperando…': 'Waiting…', 'Esperando la autorización en GitHub…': 'Waiting for the authorisation on GitHub…',
    'Pidiendo un código a GitHub…': 'Requesting a code from GitHub…', 'GitHub Copilot conectado.': 'GitHub Copilot connected.',
    'El código ha caducado. Cierra y vuelve a empezar.': 'The code has expired. Close and start again.',
    'Conectar GitHub Copilot': 'Connect GitHub Copilot', 'Abre la página de GitHub de abajo.': 'Open the GitHub page below.', 'Introduce el código.': 'Enter the code.',
    'Autoriza el acceso. Esta ventana continúa sola.': 'Authorise the access. This window continues on its own.', 'Código para GitHub': 'Code for GitHub',
    'Conectar Claude': 'Connect Claude', 'Abre la página de Claude de abajo e inicia sesión (Google o e-mail).': 'Open the Claude page below and sign in (Google or e-mail).',
    'Autoriza el acceso y copia el código que te muestra.': 'Authorise the access and copy the code it shows you.', 'Pégalo aquí.': 'Paste it here.',
    'Preparando el inicio de sesión…': 'Preparing the sign-in…', 'Código de Claude': 'Claude code', 'Conectar': 'Connect',
    'Comprobando el código…': 'Checking the code…', 'Esperando el código…': 'Waiting for the code…', 'Claude conectado.': 'Claude connected.',
    'Claude no ha aceptado el código.': 'Claude did not accept the code.', 'Abrir el inicio de sesión de Claude': 'Open the Claude sign-in',
    'Inventario': 'Inventory', 'Análisis estático': 'Static analysis', 'Contexto del proyecto (IA)': 'Project context (AI)', 'Verificación (IA)': 'Verification (AI)',
    'Descubrimiento (IA)': 'Discovery (AI)', 'Aplicación de cambios': 'Applying changes', 'Informes': 'Reports',
    'Limpieza anterior sin terminar': 'Previous cleanup unfinished', 'Sin limpiar todavía': 'Not cleaned yet', 'simulación': 'dry run',
    'No hay aplicaciones en la carpeta de trabajo. Copia aquí la carpeta de cada aplicación legacy y pulsa «Actualizar lista».': 'There are no applications in the working folder. Copy each legacy application\'s folder here and press “Refresh list”.',
    'Esperando a SonarQube: la limpieza empieza por su análisis.': 'Waiting for SonarQube: the cleanup starts with its analysis.',
    'Elige qué aplicación quieres limpiar.': 'Pick which application you want to clean.', 'Elige una aplicación.': 'Pick an application.',
    'Deteniendo…': 'Stopping…', 'Primero elige el modelo': 'Pick the model first', 'Primero conecta un proveedor': 'Connect a provider first', 'Modelo de .env': '.env model',
    'hallazgos': 'findings', 'veredictos de la IA': 'AI verdicts', 'ficheros eliminados': 'files removed', 'líneas quitadas': 'lines removed',
    'tokens de entrada': 'input tokens', 'tokens de salida': 'output tokens', 'coste estimado (Copilot)': 'estimated cost (Copilot)', 'coste': 'cost',
    'tiempo': 'time', 'n/d': 'n/a', ' · tarifas por token publicadas por GitHub': ' · per-token rates published by GitHub',
    'Arrancando': 'Starting', 'En curso': 'Running', 'Terminada': 'Finished', 'Con error': 'Failed', 'Detenida': 'Stopped',
    'Ejecución desconocida.': 'Unknown run.', 'Sesión caducada: recarga la página.': 'Session expired: reload the page.',
    '¿Quitar el modelo elegido aquí?\n\nPoda AI volverá a usar el proveedor y el modelo de .env.': 'Remove the model chosen here?\n\nPoda AI will go back to the provider and model from .env.',
    '¿Quitar LiteLLM?\n\nSe borran la URL y la API key guardadas aquí, se deja de usar la de .env y, si el modelo elegido era de LiteLLM, también se quita. Para volver a usarlo tendrás que pegar una API key.': 'Remove LiteLLM?\n\nThe URL and API key saved here are deleted, the one in .env stops being used and, if the chosen model was a LiteLLM one, it is removed too. To use it again you will have to paste an API key.',
    '¿Desconectar Claude?\n\nSe cierra la sesión de Claude de Poda AI (la de tu equipo no se toca) y, si el modelo elegido era de Claude, también se quita.': 'Disconnect Claude?\n\nPoda AI\'s Claude session is closed (your computer\'s own is not touched) and, if the chosen model was a Claude one, it is removed too.',
    '¿Desconectar GitHub Copilot?\n\nSe borra la sesión de GitHub guardada en este equipo, se detiene el proxy local y, si el modelo elegido era de Copilot, también se quita.': 'Disconnect GitHub Copilot?\n\nThe GitHub session saved on this computer is deleted, the local proxy is stopped and, if the chosen model was a Copilot one, it is removed too.',
    '¿Detener la limpieza? Para en cuanto acabe la llamada a la IA en curso; lo que aún no se haya aplicado no se escribe.': 'Stop the cleanup? It stops as soon as the current AI call finishes; anything not yet applied is not written.',
    // mensajes del servidor
    'Falta la URL del gateway.': 'The gateway URL is missing.', 'La URL del gateway debe empezar por http:// o https://.': 'The gateway URL must start with http:// or https://.',
    "La URL contiene '#': parece la de un portal web, no la de la API.": "The URL contains '#': it looks like a web portal, not the API.",
    'La URL del gateway todavía es el valor de ejemplo.': 'The gateway URL is still the example value.', 'Falta la API key (obligatoria).': 'The API key is missing (required).',
    'La API key todavía es el valor de ejemplo.': 'The API key is still the example value.', 'Quitado: pega una API key para volver a usarlo.': 'Removed: paste an API key to use it again.',
    'Sin conectar: conecta con GitHub o pega un token.': 'Not connected: connect with GitHub or paste a token.',
    'No hay sesión de GitHub Copilot: conéctala con GitHub.': 'There is no GitHub Copilot session: connect it with GitHub.',
    'Sin conectar: inicia sesión con Claude (Google o e-mail).': 'Not connected: sign in with Claude (Google or e-mail).',
    'Sin sesión de Claude: inicia sesión.': 'No Claude session: sign in.', 'Falta el comando `claude`: instala Claude Code.': 'The `claude` command is missing: install Claude Code.',
    'No se encuentra el comando `claude`: instala Claude Code.': 'The `claude` command was not found: install Claude Code.',
    'Esa conexión ya no está en curso: empieza de nuevo.': 'That sign-in is no longer in progress: start again.', 'Esa conexión ya no está en curso.': 'That sign-in is no longer in progress.',
    'Pega el código que te muestra Claude.': 'Paste the code Claude shows you.', 'El inicio de sesión ha caducado. Empieza de nuevo.': 'The sign-in has expired. Start again.',
    'Claude no tiene sesión iniciada: conéctala en el dashboard.': 'Claude is not signed in: connect it in the dashboard.',
    'Tu cuenta de Claude no ha respondido con ningún modelo: revisa tu plan o tu límite de uso.': 'Your Claude account did not answer with any model: check your plan or usage limit.',
    'La API key no tiene ningún modelo disponible en el gateway.': 'The API key has no model available on the gateway.', 'GitHub Copilot no devolvió ningún modelo para esta cuenta.': 'GitHub Copilot returned no model for this account.',
    'El token está vacío.': 'The token is empty.', 'El token contiene espacios: pégalo de nuevo sin comillas.': 'The token contains spaces: paste it again without quotes.',
    'Inicio de sesión desconocido o terminado: vuelve a empezar.': 'Unknown or finished sign-in: start again.',
    'El código ha caducado sin autorizarse. Vuelve a empezar.': 'The code expired without being authorised. Start again.', 'Se ha cancelado la autorización en GitHub.': 'The authorisation was cancelled on GitHub.',
    'No hay ninguna ejecución en curso.': 'There is no run in progress.',
    'No hay ningún SonarQube configurado (SONAR_HOST_URL). Con Docker se levanta solo con docker compose up.': 'No SonarQube is configured (SONAR_HOST_URL). With Docker it starts on its own with docker compose up.',
    'SonarQube todavía no responde. Si acabas de levantar los contenedores, la primera vez tarda 1–3 minutos.': 'SonarQube is not responding yet. If you have just started the containers, the first time takes 1–3 minutes.',
    'Hay una limpieza en marcha: los proveedores no se pueden cambiar, quitar ni actualizar hasta que termine. El modelo sí se puede cambiar; se aplicará a la próxima limpieza.': 'A cleanup is running: providers cannot be changed, removed or refreshed until it finishes. The model can still be changed; it will apply to the next cleanup.',
  };
  const RULES = [
    [/^Conectado como (.+)$/, 'Connected as $1'], [/^(\d+) modelo\(s\)( \(en caché\))?$/, (m, n, c) => `${n} model(s)${c ? ' (cached)' : ''}`],
    [/^(.+): (\d+) modelo\(s\) disponibles\.$/, '$1: $2 model(s) available.'],
    [/^Listando los modelos de (.+)…$/, 'Listing the $1 models…'], [/^No se pudieron listar los modelos de (.+): (.+)$/, 'Could not list the $1 models: $2'],
    [/^Guardando (.+)…$/, (m, a) => `Saving ${tr(a)}…`], [/^No se ha guardado (.+): (.+)$/, (m, a, e) => `Could not save ${tr(a)}: ${tr(e)}`],
    [/^No se pudo leer el estado de los proveedores: (.+)$/, 'Could not read the providers\' status: $1'], [/^No se pudo leer el modelo elegido: (.+)$/, 'Could not read the chosen model: $1'],
    [/^No se pudieron borrar los valores: (.+)$/, 'Could not delete the values: $1'], [/^No se pudo desconectar: (.+)$/, 'Could not disconnect: $1'],
    [/^No se pudo iniciar la conexión: (.+)$/, 'Could not start the sign-in: $1'], [/^No se pudo completar la conexión: (.+)$/, 'Could not complete the sign-in: $1'],
    [/^No se pudo leer la carpeta de aplicaciones: (.+)$/, 'Could not read the applications folder: $1'], [/^No se pudo iniciar: (.+)$/, 'Could not start: $1'], [/^No se pudo detener: (.+)$/, 'Could not stop: $1'],
    [/^(.+) no ofrece modelos todavía: pulsa Actualizar modelos\.$/, '$1 offers no models yet: press Refresh models.'], [/^— (.+) ya no está disponible —$/, '— $1 is no longer available —'],
    [/^(.+) \(sin conectar\)$/, '$1 (not connected)'], [/^Listo \(versión (.+)\)$/, 'Ready (version $1)'], [/^\(versión (.+)\)$/, '(version $1)'],
    [/^API key (de \.env|guardada): déjalo vacío para conservarla$/, (m, w) => `API key ${w === 'guardada' ? 'saved' : 'from .env'}: leave empty to keep it`],
    [/^Limpieza de (.+) terminada\.$/, 'Cleanup of $1 finished.'], [/^Limpieza de (.+)$/, 'Cleanup of $1'], [/^Limpiando (.+)…$/, 'Cleaning $1…'],
    [/^Arrancando la limpieza de (.+)…$/, 'Starting the cleanup of $1…'], [/^Listo para limpiar (.+)\.$/, 'Ready to clean $1.'],
    [/^La ejecución terminó con estado (.+)\.$/, 'The run ended with status $1.'], [/^Modelo: (.+)$/, 'Model: $1'], [/^Última: (.+)$/, 'Last: $1'],
    [/^([\d.,]+) ficheros$/, '$1 files'], [/^([\d.,]+) líneas$/, '$1 lines'], [/^Sin respuesta del servidor \((.+)\); reintentando…$/, 'No answer from the server ($1); retrying…'],
    [/^Esperando la autorización en GitHub… \(código válido (.+)\)$/, 'Waiting for the authorisation on GitHub… (code valid $1)'], [/^Esperando… \((.+)\)$/, 'Waiting… ($1)'],
    [/^Conexión terminada: (.+)$/, 'Sign-in ended: $1'], [/^Recortado: el fichero completo está en (.+)$/, 'Truncated: the full file is at $1'],
    [/^Todavía no hay (.+) para (.+)\.$/, 'There is no $1 yet for $2.'], [/^No existe la aplicación «(.+)» en (.+)\.$/, 'The application “$1” does not exist in $2.'],
    [/^Ya hay una ejecución en curso \((.+)\)\. Espera a que termine o detenla\.$/, 'A run is already in progress ($1). Wait for it to finish or stop it.'],
    [/^SonarQube no está listo: (.+)$/, 'SonarQube is not ready: $1'], [/^Configura primero un proveedor y un modelo\. (.+)$/, 'Set up a provider and a model first. $1'],
    [/^SonarQube está arrancando \((.+)\)\. Espera un momento\.$/, 'SonarQube is starting ($1). Wait a moment.'],
    [/^Gateway (.+)$/, 'Gateway $1'], [/^Límite de uso de tu plan de Claude alcanzado: (.+)$/, 'Your Claude plan\'s usage limit was reached: $1'],
    [/^(.+) por M tokens$/, '$1 per M tokens'],
  ];
  const SYMBOLS = /^([✔✖⚠▲⏳○·◐⤼—\s]+)/;

  function tr(text) {
    if (typeof text !== 'string' || !text.trim()) return text;
    if (EN[text] !== undefined) return EN[text];
    const lead = text.match(/^\s*/)[0], tail = text.match(/\s*$/)[0];
    const core = text.trim().replace(/\s+/g, ' ');
    if (EN[core] !== undefined) return lead + EN[core] + tail;
    if (core.includes(' · ')) return lead + core.split(' · ').map(tr).join(' · ') + tail;
    for (const [re, to] of RULES) {
      if (re.test(core)) return lead + core.replace(re, to) + tail;
    }
    const sym = core.match(SYMBOLS);
    if (sym && core.length > sym[0].length) return lead + sym[0] + tr(core.slice(sym[0].length)) + tail;
    return text;
  }

  let lang = 'en';
  try { lang = localStorage.getItem('poda-lang') === 'es' ? 'es' : 'en'; } catch { /* storage blocked */ }
  const SKIP = '#log, #report, script, style, textarea, code[data-raw]';
  const textStore = new WeakMap(), attrStore = new WeakMap();
  const ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];
  let busy = false;

  function doText(node) {
    const rec = textStore.get(node);
    if (lang === 'en') {
      if (rec && node.data === rec.en) return;
      const en = tr(node.data);
      textStore.set(node, { es: node.data, en });
      if (en !== node.data) node.data = en;
    } else if (rec && node.data === rec.en) {
      node.data = rec.es;
      textStore.delete(node);
    }
  }
  function doAttrs(el) {
    let rec = attrStore.get(el);
    for (const a of ATTRS) {
      if (!el.hasAttribute(a)) continue;
      const cur = el.getAttribute(a);
      if (lang === 'en') {
        if (rec && rec[a] && rec[a].en === cur) continue;
        const en = tr(cur);
        rec = rec || {}; rec[a] = { es: cur, en }; attrStore.set(el, rec);
        if (en !== cur) el.setAttribute(a, en);
      } else if (rec && rec[a] && rec[a].en === cur) {
        el.setAttribute(a, rec[a].es); delete rec[a];
      }
    }
  }
  function walk(root) {
    if (root.nodeType === 3) { if (!root.parentElement?.closest(SKIP)) doText(root); return; }
    if (root.nodeType !== 1 || root.matches?.(SKIP) || root.closest?.(SKIP)) return;
    doAttrs(root);
    root.querySelectorAll?.('*').forEach(el => { if (!el.closest(SKIP)) doAttrs(el); });
    const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = it.nextNode(); n; n = it.nextNode()) if (!n.parentElement?.closest(SKIP)) doText(n);
  }
  function apply() {
    busy = true;
    try { walk(document.body); document.title = 'Poda AI'; document.documentElement.lang = lang === 'en' ? 'en-GB' : 'es'; }
    finally { busy = false; }
  }
  new MutationObserver((records) => {
    if (busy) return;
    busy = true;
    try {
      for (const r of records) {
        if (r.type === 'characterData') walk(r.target);
        else if (r.type === 'attributes') doAttrs(r.target);
        else r.addedNodes.forEach(walk);
      }
    } finally { busy = false; }
  }).observe(document.body, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ATTRS });

  const nativeConfirm = window.confirm.bind(window);
  window.confirm = (message) => nativeConfirm(lang === 'en' ? tr(String(message)) : message);

  window.PodaLang = {
    get: () => lang,
    locale: () => (lang === 'en' ? 'en-GB' : 'es-ES'),
    output: () => (lang === 'en' ? 'English' : 'Spanish'),
    set(next) {
      lang = next === 'es' ? 'es' : 'en';
      try { localStorage.setItem('poda-lang', lang); } catch { /* only this visit */ }
      apply();
      document.querySelectorAll('[data-lang]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.lang === lang)));
    },
    tr,
  };
  document.documentElement.lang = lang === 'en' ? 'en-GB' : 'es';
  document.querySelectorAll('[data-lang]').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.lang === lang));
    b.addEventListener('click', () => window.PodaLang.set(b.dataset.lang));
  });
  apply();
})();
