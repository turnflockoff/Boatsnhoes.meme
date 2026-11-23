// Boats N Hoes: public logbook (/reports/). Lists published periods from /reports/index.json.
// Builds every element with textContent, never innerHTML, so nothing in the index can run.
(() => {
  const $ = (s) => document.querySelector(s);

  // Mobile menu
  const menu = $('#menubtn'), nav = $('#navlinks');
  menu.addEventListener('click', () => menu.setAttribute('aria-expanded', String(nav.classList.toggle('open'))));
  nav.querySelectorAll('a').forEach((a) => a.addEventListener('click', () => { nav.classList.remove('open'); menu.setAttribute('aria-expanded', 'false'); }));

  async function copy(text, btn) {
    try { await navigator.clipboard.writeText(text); } catch {
      const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select();
      document.execCommand('copy'); t.remove();
    }
    const old = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = old; }, 1400);
  }
  document.querySelectorAll('.cmd .copy').forEach((b) => b.addEventListener('click', () => copy(b.parentElement.querySelector('code').textContent, b)));

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const day = (t) => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  const num = (n) => Number(n || 0).toLocaleString('en-US');
  const safePath = (p) => typeof p === 'string' && /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(p) && !p.includes('..');
  const kb = (b) => (b >= 1024 * 1024 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');

  function card(p) {
    const box = el('article', 'period');
    box.appendChild(el('span', 'tagline', 'Period'));
    box.appendChild(el('h3', '', day(p.since) + ' → ' + day(p.until)));
    const stats = el('ul', 'stats');
    [['Rigs', num(p.rigs)], ['Results', num(p.accepted_results)], ['Simulated points', num(p.payable_points)],
     ['Scoring', String(p.scoring_version || '?')], ['Posted', day(p.published_at)]].forEach(([k, v]) => {
      const li = el('li'); li.appendChild(document.createTextNode(k + ': ')); li.appendChild(el('b', '', v)); stats.appendChild(li);
    });
    box.appendChild(stats);
    if (p.previous && p.previous.history_unchanged) {
      box.appendChild(el('p', 'fine ok', '✓ Earlier history unchanged since the previous period'));
    }
    const files = el('div', 'files');
    [['report', 'Signed report (JSON)'], ['ledger_copy', 'Ledger copy (SQLite)']].forEach(([key, label]) => {
      const f = (p.files || {})[key];
      if (!f || !safePath(f.path)) return;
      const a = el('a', 'btn ghost', label + ' · ' + kb(f.bytes || 0));
      a.href = '/reports/' + f.path; a.setAttribute('download', '');
      files.appendChild(a);
    });
    box.appendChild(files);
    box.appendChild(el('code', 'hash', 'report sha256 ' + String(p.report_sha256 || '')));
    return box;
  }

  async function load() {
    const list = $('#periodlist');
    let index;
    try {
      const r = await fetch('/reports/index.json', { cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      index = await r.json();
    } catch {
      return; // keep the static text: it already points to GitHub
    }
    if (!index || index.format !== 'bnh-reports-index/1' || !Array.isArray(index.periods)) return;
    if (index.coordinator && index.coordinator.public_key) {
      const line = $('#keyline'); line.textContent = 'Coordinator signing key, fingerprint ';
      line.appendChild(el('b', '', String(index.coordinator.fingerprint || '')));
      line.appendChild(document.createTextNode(': '));
      line.appendChild(el('code', '', String(index.coordinator.public_key)));
    }
    if (!index.periods.length) return;
    list.replaceChildren(...index.periods.slice().sort((a, b) => b.until - a.until).map(card));
  }
  load();
})();
