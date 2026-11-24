(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const sleep = (ms) => new Promise(r => setTimeout(r, reduced ? 0 : ms));
  const PHASES = ['Inventory', 'SonarQube', 'Static analysis', 'Context (AI)', 'Verification (AI)', 'Discovery (AI)', 'Apply', 'Reports'];

  // theme
  const toggle = $('#themeToggle');
  const paint = () => toggle.setAttribute('aria-pressed', String(document.documentElement.dataset.theme === 'dark'));
  toggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('poda-site-theme', next); } catch { /* only this visit */ }
    paint();
  });
  paint();

  // reveal the phase cards as they scroll into view
  const cards = $$('#phases li');
  if (reduced || !('IntersectionObserver' in window)) cards.forEach(c => c.classList.add('in'));
  else {
    const io = new IntersectionObserver((entries) => entries.forEach(e => {
      if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    }), { threshold: 0.2 });
    cards.forEach((c, i) => { c.style.transitionDelay = `${(i % 4) * 70}ms`; io.observe(c); });
    setTimeout(() => cards.forEach(c => c.classList.add('in')), 4000);   // never leave a card hidden
  }

  // copy command
  $('#copyCmd').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    try { await navigator.clipboard.writeText($('#cmds').innerText); btn.textContent = 'Copied'; }
    catch { btn.textContent = 'Press Ctrl+C'; }
    setTimeout(() => { btn.textContent = 'Copy'; }, 1600);
  });

  // ---- code demo: the AI judges each suspicious line, then the dead ones fold away ----
  const strip = $('#phaseStrip');
  strip.innerHTML = PHASES.map(p => `<li>${p}</li>`).join('');
  const lines = $$('#code .ln');
  const dead = lines.filter(l => l.dataset.dead);
  const badges = () => dead.forEach(l => { l.dataset.badge = window.PodaI18n.t(l.dataset.dead === 'remove' ? 'remove · high' : 'review'); });
  badges();
  document.addEventListener('sitelang', badges);
  const count = (el, to) => { el.textContent = String(to); };
  let runId = 0, playing = false;

  function reset() {
    lines.forEach(l => l.classList.remove('judged', 'striking', 'gone'));
    $$('li', strip).forEach(li => li.classList.remove('on', 'done'));
    $('#codeBar').style.width = '0';
    ['kFindings', 'kVerdicts', 'kLines', 'kReview'].forEach(id => count($('#' + id), 0));
  }
  async function playCode() {
    const mine = ++runId; playing = true;
    reset();
    const live = () => mine === runId;
    const step = async (i, ms) => {
      $$('li', strip).forEach((li, n) => { li.classList.toggle('on', n === i); li.classList.toggle('done', n < i); });
      $('#codeBar').style.width = `${Math.round(((i + 1) / PHASES.length) * 100)}%`;
      await sleep(ms);
    };
    $('#codeNote').textContent = 'Running the cleanup…';
    for (let i = 0; i < 3; i++) { if (!live()) return; await step(i, 650); }
    // static analysis found candidates
    count($('#kFindings'), dead.length);
    if (!live()) return; await step(3, 600);
    if (!live()) return; await step(4, 300);
    let verdicts = 0;
    for (const l of dead) {                               // verification: one verdict per finding
      if (!live()) return;
      l.classList.add('judged'); verdicts++; count($('#kVerdicts'), verdicts); await sleep(420);
    }
    count($('#kReview'), dead.filter(l => l.dataset.dead === 'review').length);
    if (!live()) return; await step(5, 600);
    if (!live()) return; await step(6, 200);
    const removable = dead.filter(l => l.dataset.dead === 'remove');
    removable.forEach(l => l.classList.add('striking'));   // apply: strike, then fold away
    await sleep(700); if (!live()) return;
    let removed = 0;
    for (const l of removable) { l.classList.add('gone'); removed++; count($('#kLines'), removed); await sleep(160); }
    if (!live()) return; await step(7, 500);
    $$('li', strip).forEach(li => { li.classList.remove('on'); li.classList.add('done'); });
    $('#codeNote').textContent = `Done: ${removable.length} lines removed from the copy, 1 left for review. The original was never touched.`;
    playing = false;
  }
  $('#codeReplay').addEventListener('click', playCode);
  if (reduced) {
    reset();
    dead.forEach(l => l.classList.add('judged'));
    dead.filter(l => l.dataset.dead === 'remove').forEach(l => l.classList.add('gone'));
    $$('li', strip).forEach(li => li.classList.add('done'));
    $('#codeBar').style.width = '100%';
    count($('#kFindings'), dead.length); count($('#kVerdicts'), dead.length);
    count($('#kLines'), dead.filter(l => l.dataset.dead === 'remove').length); count($('#kReview'), 1);
    $('#codeNote').textContent = `Done: ${dead.filter(l => l.dataset.dead === 'remove').length} lines removed from the copy, 1 left for review. The original was never touched.`;
  } else if ('IntersectionObserver' in window) {
    let started = false;
    new IntersectionObserver((entries, obs) => {
      if (!started && entries.some(e => e.isIntersecting)) { started = true; obs.disconnect(); playCode(); }
    }, { threshold: 0.35 }).observe($('#codeDemo'));
  } else playCode();

  // ---- dashboard mock: tabs + a simulated run ----
  const tabs = $$('.dstep');
  const select = (n) => {
    tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.step === String(n))));
    [1, 2, 3].forEach(i => { $('#dp' + i).hidden = i !== n; });
  };
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => select(Number(t.dataset.step)));
    t.addEventListener('keydown', (e) => {
      const to = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : -1;
      if (to >= 0 && to < tabs.length) { e.preventDefault(); tabs[to].focus(); select(to + 1); }
    });
  });
  const mini = $('#miniPhases');
  mini.innerHTML = PHASES.map(p => `<li>${p}</li>`).join('');
  const LOG = [
    ['', 'Inventory: 412 files classified, 38,204 lines of code'],
    ['', 'SonarQube: unused-code rules → 1,096 issues'],
    ['', 'Static analysis: 214 more candidates, merged with SonarQube'],
    ['', 'Context: entry points and protected files identified'],
    ['ok2', 'Verification: 1,310 findings judged' + String.fromCharCode(10) + '  ▲ 87 left for manual review (medium confidence)'],
    ['', 'Discovery: 6 forever-off flags, 2 dead mappings'],
    ['rm', 'Apply: 91 files deleted, 2,847 lines removed from the copy'],
    ['ok2', 'Reports: poda_report.md, findings.json, execution_metrics.md'],
  ];
  let dashRun = 0;
  $('#dashRun').addEventListener('click', async () => {
    const mine = ++dashRun, log = $('#dashLog'), chip = $('#dashChip'), bar = $('#dashBar'), items = $$('li', mini);
    chip.textContent = 'Running'; chip.classList.add('running'); log.textContent = ''; bar.style.width = '0';
    items.forEach(li => li.className = '');
    $('#dashRun').disabled = true;
    for (let i = 0; i < PHASES.length; i++) {
      if (mine !== dashRun) return;
      items.forEach((li, n) => { li.className = n < i ? 'done' : n === i ? 'run' : ''; });
      bar.style.width = `${Math.round(((i + 1) / PHASES.length) * 100)}%`;
      const [cls, text] = LOG[i] || ['', ''];
      const row = document.createElement('div'); row.className = cls; row.textContent = text;
      log.appendChild(row);
      log.scrollTop = log.scrollHeight;
      await sleep(i === 4 ? 900 : 600);
    }
    items.forEach(li => { li.className = 'done'; });
    chip.textContent = 'Finished'; chip.classList.remove('running');
    $('#dashRun').disabled = false; $('#dashRun').textContent = 'Run again';
  });
})();
