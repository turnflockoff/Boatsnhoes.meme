// Boats N Hoes — GPU contributor page. Everything here runs locally in the browser.
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

  // --- Device check ---------------------------------------------------------
  async function webgpuInfo() {
    if (!('gpu' in navigator)) return { supported: false };
    try {
      const adapter = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' });
      if (!adapter) return { supported: true, adapter: false };
      let info = adapter.info;
      if (!info && adapter.requestAdapterInfo) info = await adapter.requestAdapterInfo();
      info = info || {};
      return {
        supported: true, adapter: true,
        vendor: info.vendor || '', architecture: info.architecture || '',
        device: info.device || '', description: info.description || '',
        fallback: Boolean(adapter.isFallbackAdapter || info.isFallbackAdapter),
        maxBufferMB: Math.round((adapter.limits?.maxBufferSize || 0) / 1048576),
      };
    } catch (e) { return { supported: true, adapter: false, error: String(e.message || e) }; }
  }

  function webglInfo() {
    try {
      const c = document.createElement('canvas');
      const gl = c.getContext('webgl2') || c.getContext('webgl');
      if (!gl) return { supported: false };
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      return {
        supported: true, version: gl instanceof (window.WebGL2RenderingContext || Object) ? 'WebGL 2' : 'WebGL 1',
        vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
        renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      };
    } catch { return { supported: false }; }
  }

  function classify(text, mobile) {
    const t = text.toLowerCase();
    if (mobile) return ['no', 'Phone or tablet', 'Fine for browsing the project, but not a candidate for a worker node.'];
    if (/swiftshader|llvmpipe|softpipe|basic render|microsoft basic|software/.test(t)) return ['warn', 'No hardware GPU visible', 'Your browser is using software rendering. Hardware acceleration may be off — the Linux check in step 2 will tell the real story.'];
    if (/rx ?(460|470|480|550|560|570|580|590)|polaris|ellesmere|baffin|lexa/.test(t)) return ['yes', 'Polaris-family AMD GPU', 'This is the exact card family we are testing first. Run step 2 and join the tester list.'];
    if (/radeon|amd|ati |nvidia|geforce|rtx|gtx|quadro|intel\(r\) arc| arc /.test(t)) return ['yes', 'Discrete GPU detected', 'Worth testing. Run the Linux check in step 2 to confirm Vulkan support and VRAM.'];
    if (/intel|uhd|iris|apple|m1|m2|m3|m4|mali|adreno|powervr/.test(t)) return ['warn', 'Integrated GPU', 'Good for learning and following along. Probably too small for worker jobs.'];
    return ['warn', 'GPU not identified', 'Your browser hid the details (common in privacy-focused browsers). Step 2 gives the real answer.'];
  }

  let reportText = '';
  $('#runcheck').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget; btn.disabled = true; btn.textContent = 'Checking…';
    const [gpu, gl] = await Promise.all([webgpuInfo(), Promise.resolve(webglInfo())]);
    const uaMobile = navigator.userAgentData?.mobile ?? /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
    const platform = navigator.userAgentData?.platform || navigator.platform || 'unknown';
    const rows = [
      ['Graphics (WebGL)', gl.supported ? `${gl.renderer || 'hidden'}` : 'Not available'],
      ['GPU vendor', gpu.vendor || gl.vendor || 'hidden by browser'],
      ['WebGPU', !gpu.supported ? 'Not supported in this browser' : gpu.adapter ? `Yes${gpu.fallback ? ' (software fallback)' : ''}${gpu.architecture ? ' · ' + gpu.architecture : ''}${gpu.description ? ' · ' + gpu.description : ''}` : 'Supported, but no adapter'],
      ['Max GPU buffer', gpu.maxBufferMB ? `${gpu.maxBufferMB} MB (browser limit, not VRAM)` : '—'],
      ['CPU threads', String(navigator.hardwareConcurrency || '?')],
      ['Device memory', navigator.deviceMemory ? `≈${navigator.deviceMemory} GB (rounded by browser)` : 'hidden'],
      ['Platform', `${platform}${uaMobile ? ' · mobile' : ''}`],
    ];
    const signature = [gl.renderer, gl.vendor, gpu.vendor, gpu.architecture, gpu.description].join(' ');
    const [level, title, detail] = classify(signature, uaMobile);

    const v = $('#verdict'); v.className = 'verdict ' + level; v.replaceChildren();
    const h = document.createElement('strong'); h.textContent = title;
    const p = document.createElement('span'); p.textContent = detail;
    v.append(h, p);
    const dl = $('#facts'); dl.replaceChildren();
    rows.forEach(([k, val]) => { const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = k; dd.textContent = val; dl.append(dt, dd); });

    reportText = ['Boats N Hoes browser check', `Result: ${title}`, ...rows.map(([k, val]) => `${k}: ${val}`), `Checked: ${new Date().toISOString()}`].join('\n');
    $('#report').hidden = false; btn.disabled = false; btn.textContent = 'Check again';
  });

  $('#copyreport').addEventListener('click', (e) => copy(reportText, e.currentTarget));
  $('#attachreport').addEventListener('click', () => {
    const f = $('#reportfield'); f.value = (f.value ? f.value + '\n\n' : '') + reportText;
    $('#join').scrollIntoView({ behavior: 'smooth' });
  });
})();
