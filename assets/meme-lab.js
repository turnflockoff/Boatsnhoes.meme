// Boats N Hoes Meme Lab: an in-browser meme maker and the approved-memes gallery.
// Everything is drawn locally on a <canvas>. Nothing is uploaded unless the visitor
// chooses to send a meme through the submission form on the same page.
(() => {
  const $ = (id) => document.getElementById(id);
  const canvas = $('meme-canvas');
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  const top = $('meme-top'), bottom = $('meme-bottom'), size = $('meme-size');
  const format = $('meme-format'), ink = $('meme-ink'), mark = $('meme-mark');
  const ownFile = $('meme-own'), status = $('meme-status');
  const FORMATS = { square: [1080, 1080], wide: [1200, 675], tall: [1080, 1350] };
  const ORANGE = ['#ffb21e', '#ff493e'];
  const images = {};
  let template = 'boat';
  let own = null;

  // SVGs need a width/height to draw on a canvas in every browser, so they are loaded as
  // text, given one, and drawn from a data: URL (allowed by the site's image policy).
  function loadSvg(name, url, w, h) {
    return fetch(url).then((r) => (r.ok ? r.text() : Promise.reject(r.status))).then((text) => {
      const sized = text.replace('<svg ', `<svg width="${w}" height="${h}" `);
      return loadImage(name, 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(sized));
    });
  }
  function loadImage(name, src) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => { images[name] = img; resolve(img); };
      img.onerror = () => resolve(null);
      img.src = src;
    });
  }

  function gradient(w, h) {
    const g = ctx.createLinearGradient(0, 0, w, h);
    g.addColorStop(0, ORANGE[0]); g.addColorStop(1, ORANGE[1]);
    return g;
  }
  function darkBackground(w, h) {
    ctx.fillStyle = '#090705'; ctx.fillRect(0, 0, w, h);
    const glow = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) * 0.6);
    glow.addColorStop(0, '#3a1a0b'); glow.addColorStop(1, '#090705');
    ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = '#2a211b'; ctx.lineWidth = 2;
    for (let r = 1; r <= 5; r++) {
      ctx.beginPath(); ctx.arc(w / 2, h / 2, Math.min(w, h) * 0.12 * r, 0, Math.PI * 2); ctx.stroke();
    }
  }
  function fit(img, w, h, scale, cover) {
    const k = (cover ? Math.max : Math.min)(w / img.width, h / img.height) * scale;
    const dw = img.width * k, dh = img.height * k;
    ctx.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh);
  }

  function drawBackground(w, h) {
    if (template === 'boat') {
      darkBackground(w, h);
      if (images.boat) fit(images.boat, w, h, 0.62, false);
    } else if (template === 'logo') {
      ctx.fillStyle = gradient(w, h); ctx.fillRect(0, 0, w, h);
      if (images.mark) fit(images.mark, w, h, 0.5, false);
    } else if (template === 'card') {
      ctx.fillStyle = '#090705'; ctx.fillRect(0, 0, w, h);
      if (images.card) fit(images.card, w, h, 1, true);
    } else if (template === 'orange') {
      ctx.fillStyle = gradient(w, h); ctx.fillRect(0, 0, w, h);
    } else if (template === 'own' && own) {
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, w, h);
      fit(own, w, h, 1, true);
    } else {
      darkBackground(w, h);
    }
  }

  function wrap(text, maxWidth) {
    const words = text.split(/\s+/).filter(Boolean);
    const lines = [];
    let line = '';
    for (const word of words) {
      const next = line ? line + ' ' + word : word;
      if (ctx.measureText(next).width <= maxWidth || !line) line = next;
      else { lines.push(line); line = word; }
    }
    if (line) lines.push(line);
    return lines.slice(0, 4);
  }

  function drawText(text, w, h, atTop) {
    if (!text.trim()) return;
    const px = Math.round((Number(size.value) / 1080) * w);
    ctx.font = `900 ${px}px Impact, "Arial Black", "Helvetica Neue", sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = atTop ? 'top' : 'bottom';
    ctx.lineJoin = 'round';
    const lines = wrap(text.toUpperCase(), w * 0.9);
    const lh = px * 1.08;
    const dark = ink.value === 'black';
    ctx.lineWidth = Math.max(4, px / 8);
    ctx.strokeStyle = dark ? '#ffffff' : '#000000';
    ctx.fillStyle = dark ? '#000000' : '#ffffff';
    lines.forEach((line, i) => {
      const y = atTop ? h * 0.04 + i * lh : h * 0.96 - (lines.length - 1 - i) * lh;
      ctx.strokeText(line, w / 2, y);
      ctx.fillText(line, w / 2, y);
    });
  }

  function draw() {
    const [w, h] = FORMATS[format.value] || FORMATS.square;
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.clearRect(0, 0, w, h);
    drawBackground(w, h);
    drawText(top.value, w, h, true);
    drawText(bottom.value, w, h, false);
    if (mark.checked) {
      const px = Math.round(w / 45);
      ctx.font = `700 ${px}px ui-monospace, "SF Mono", monospace`;
      ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
      ctx.fillStyle = 'rgba(255,255,255,.75)';
      ctx.fillText('boatsnhoes.meme', w - px, h - px * 0.6);
    }
    const words = [top.value, bottom.value].filter((t) => t.trim()).join(' / ');
    canvas.setAttribute('aria-label', 'Meme preview' + (words ? ': ' + words : ' with no text yet'));
  }

  function choose(name) {
    template = name;
    document.querySelectorAll('[data-template]').forEach((b) =>
      b.setAttribute('aria-pressed', String(b.dataset.template === name)));
    $('meme-own-row').hidden = name !== 'own';
    draw();
  }

  function toBlob() {
    return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  }

  document.querySelectorAll('[data-template]').forEach((b) =>
    b.addEventListener('click', () => choose(b.dataset.template)));
  [top, bottom, size, format, ink, mark].forEach((el) => el.addEventListener('input', draw));

  ownFile.addEventListener('change', () => {
    const file = ownFile.files && ownFile.files[0];
    if (!file) return;
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type) || file.size > 15 * 1024 * 1024) {
      status.textContent = 'Use a PNG, JPEG, GIF or WebP picture under 15 MB.';
      return;
    }
    const reader = new FileReader(); // read locally; the picture never leaves this browser
    reader.onload = () => loadImage('own', reader.result).then((img) => {
      own = img;
      status.textContent = img ? 'Your picture is loaded (it stays on your device).' : 'That picture could not be opened.';
      draw();
    });
    reader.readAsDataURL(file);
  });

  $('meme-download').addEventListener('click', async () => {
    const blob = await toBlob();
    if (!blob) { status.textContent = 'Your browser could not make the picture.'; return; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'bnh-meme.png';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    status.textContent = 'Saved as bnh-meme.png.';
  });

  $('meme-use').addEventListener('click', async () => {
    const input = $('submit-file');
    const blob = await toBlob();
    try {
      const dt = new DataTransfer();
      dt.items.add(new File([blob], 'bnh-meme.png', { type: 'image/png' }));
      input.files = dt.files;
      status.textContent = 'Added to the submission form below.';
      $('submit').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      input.focus({ preventScroll: true });
    } catch (e) {
      status.textContent = 'Your browser can\'t attach it directly: download it, then choose the file in the form below.';
    }
  });

  Promise.all([
    loadSvg('boat', '/assets/boat-orange.svg', 800, 600),
    loadSvg('mark', '/assets/mark-orange.svg', 512, 512),
    loadImage('card', '/assets/og-home.png'),
  ]).then(draw);
  choose('boat');

  // Approved memes: the owner adds files to /assets/memes/ and lists them in gallery.json.
  const gallery = $('meme-gallery');
  if (gallery) {
    fetch('/assets/memes/gallery.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : { memes: [] }))
      .catch(() => ({ memes: [] }))
      .then((data) => {
        const memes = (data && Array.isArray(data.memes) ? data.memes : [])
          .filter((m) => m && typeof m.file === 'string' && /^\/assets\/memes\/[A-Za-z0-9._-]+\.(png|jpe?g|gif|webp)$/.test(m.file)
            && typeof m.alt === 'string' && m.alt.trim());
        if (!memes.length) return;
        gallery.textContent = '';
        gallery.className = 'kit';
        memes.slice(0, 60).forEach((m) => {
          const fig = document.createElement('figure');
          fig.className = 'asset';
          fig.style.margin = '0';
          const img = document.createElement('img');
          img.src = m.file; img.alt = m.alt.slice(0, 300); img.loading = 'lazy';
          img.style.borderRadius = '8px';
          fig.appendChild(img);
          if (typeof m.credit === 'string' && m.credit.trim()) {
            const cap = document.createElement('figcaption');
            cap.className = 'fine';
            cap.textContent = 'By ' + m.credit.slice(0, 60);
            fig.appendChild(cap);
          }
          gallery.appendChild(fig);
        });
      });
  }
})();
