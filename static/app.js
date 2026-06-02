
const $ = id => document.getElementById(id);
let state = { tier: 'fast', aspect: '1:1', prodImage: null, generating: false, gallery: [], selected: new Set(), lastClicked: null, outputImages: [], lastPrompt: '', apiKey: '' };

// ── API Key (BYOK) ──
const API_KEY_STORAGE='cs_api_key';
function loadApiKey() { return localStorage.getItem(API_KEY_STORAGE) || ''; }
function saveApiKey(key) { localStorage.setItem(API_KEY_STORAGE, key); }
function getApiKey() { return loadApiKey(); }

function updateFetchOptions(opts = {}) {
  const key = getApiKey();
  if (key) {
    opts.headers = opts.headers || {};
    opts.headers['X-API-Key'] = key;
  }
  return opts;
}

async function validateApiKey(key) {
  try {
    const r = await fetch('/api/validate-key', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key })
    });
    return await r.json();
  } catch (e) { return { valid: false, error: e.message }; }
}

$('apikeyBtn').addEventListener('click', async () => {
  const key = $('apikeyInput').value.trim();
  if (!key) { showToast('Paste a key first', 'err'); return; }
  $('apikeyStatus').textContent = 'Checking...';
  $('apikeyStatus').className = 'apikey-status';
  const result = await validateApiKey(key);
  if (result.valid) {
    saveApiKey(key);
    $('apikeyStatus').textContent = 'Key saved ✓';
    $('apikeyStatus').className = 'apikey-status ok';
    $('apikeyInput').className = 'valid';
    showToast('API key saved', 'ok');
  } else {
    $('apikeyStatus').textContent = result.error || 'Invalid key';
    $('apikeyStatus').className = 'apikey-status err';
    $('apikeyInput').className = 'invalid';
  }
});

// Load saved key on startup
const savedKey = loadApiKey();
if (savedKey) {
  $('apikeyInput').value = savedKey;
  $('apikeyInput').className = 'valid';
  $('apikeyStatus').textContent = 'Key loaded ✓';
  $('apikeyStatus').className = 'apikey-status ok';
} else {
  // Probe server: is there a fallback key?
  fetch('/api/whoami', updateFetchOptions())
    .then(r => r.json())
    .then(info => {
      if (info.fallback_enabled && !info.byok) {
        $('apikeyStatus').textContent = 'Using shared demo key — paste your own for privacy';
        $('apikeyStatus').className = 'apikey-status warn';
        $('apikeyInput').placeholder = 'Paste your Gemini API key (recommended)...';
      } else if (info.byok_required && !info.byok) {
        $('apikeyStatus').textContent = 'Required — paste your Gemini API key to generate';
        $('apikeyStatus').className = 'apikey-status err';
        $('apikeyInput').placeholder = 'Paste your Gemini API key (required)...';
      } else {
        $('apikeyStatus').textContent = 'Key saved — ready to generate';
        $('apikeyStatus').className = 'apikey-status ok';
        $('apikeyInput').placeholder = 'Paste your Gemini API key (recommended)...';
      }
    })
    .catch(() => {});
}

const PRESETS = {
  amazon:    { prompt: 'Clean pure white background, soft shadow underneath, studio lighting, product centered, ecommerce photography, high detail, ecommerce listing photo', aspect: '1:1' },
  instagram: { prompt: 'Lifestyle flatlay on textured surface, natural soft window light from left, shallow depth of field, instagram feed aesthetic, warm tones, lifestyle product photography', aspect: '4:5' },
  email:     { prompt: 'Product on clean gradient background, dramatic side lighting, hero shot, wide composition with negative space for headline text overlay', aspect: '16:9' },
  pinterest: { prompt: 'Product in styled scene with complementary props, warm golden tones, overhead 45 degree angle, editorial style, pinterest pin composition', aspect: '2:3' },
};

// ── Chip selectors ──
function initChips(rowId, key, cls) {
  $(rowId).addEventListener('click', e => {
    const chip = e.target.closest('.' + cls);
    if (!chip) return;
    document.querySelectorAll('#' + rowId + ' .' + cls).forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    state[key] = chip.dataset.tier || chip.dataset.ratio || chip.dataset.preset;
  });
}
initChips('qualityRow', 'tier', 'quality-chip');

// Aspect chips (separate to avoid initChips eating preset clicks)
$('aspectRow').addEventListener('click', e => {
  const chip = e.target.closest('.aspect-chip');
  if (!chip) return;
  document.querySelectorAll('#aspectRow .aspect-chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active');
  state.aspect = chip.dataset.ratio;
});

// ── Presets ──
$('presetRow').addEventListener('click', e => {
  const chip = e.target.closest('.preset-chip');
  if (!chip) return;
  const key = chip.dataset.preset;
  const p = PRESETS[key];
  if (!p) return;
  $('prompt').value = p.prompt;
  state.aspect = p.aspect;
  document.querySelectorAll('.aspect-chip').forEach(c => {
    c.classList.toggle('active', c.dataset.ratio === p.aspect);
  });
  document.querySelectorAll('.preset-chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active');
});

// ── Dropzone with mouse glow ──
const dz = $('dropzone'), fi = $('fileInput');
dz.addEventListener('mousemove', e => {
  const rect = dz.getBoundingClientRect();
  dz.style.setProperty('--mx', ((e.clientX - rect.left) / rect.width * 100) + '%');
  dz.style.setProperty('--my', ((e.clientY - rect.top) / rect.height * 100) + '%');
});

const onFile = file => {
  if (!file || !file.type.startsWith('image/')) return;
  state.prodImage = file;
  $('fileName').textContent = file.name;
  const url = URL.createObjectURL(file);
  $('previewImg').src = url;
  $('previewWrap').style.display = 'block';
  $('removeBtn').style.display = 'inline-block';
  dz.querySelector('.label').textContent = 'Replace product photo';
  dz.querySelector('.icon').textContent = '🔄';
  updateGenLabel();
};
fi.addEventListener('change', e => onFile(e.target.files[0]));
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('dragover');
  onFile(e.dataTransfer.files[0]);
});

$('removeBtn').addEventListener('click', () => {
  state.prodImage = null;
  $('fileName').textContent = '';
  $('previewWrap').style.display = 'none';
  $('removeBtn').style.display = 'none';
  fi.value = '';
  dz.querySelector('.label').textContent = 'Drop product photo here';
  dz.querySelector('.icon').textContent = '📸';
  updateGenLabel();
});

function updateGenLabel() {
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  const label = state.prodImage ? 'Generate Composite' : (batch ? 'Generate 4 Images' : 'Generate Image');
  $('genLabel').textContent = label;
  // Per-tier pricing (must match data-cost on quality-chip + server _TIER_MODEL)
  const TIER_COST = { fast: 0.02, balanced: 0.045, quality: 0.09, ultra: 0.24 };
  const unit = TIER_COST[state.tier] || 0.045;
  const cost = state.prodImage ? TIER_COST.quality : (unit * count);
  const time = batch && !state.prodImage ? '~2 min' : '~30s';
  $('genMeta').textContent = '$' + cost.toFixed(2) + ' · ' + time;
}
$('batchToggle').addEventListener('change', updateGenLabel);

async function getCostToday() {
  try {
    const r = await fetch('/api/costs', updateFetchOptions());
    const d = await r.json();
    return d.today || 0;
  } catch (e) { return 0; }
}

function addToGallery(images) {
  images.forEach(img => state.gallery.push(img));
  renderGallery();
}

function renderGallery() {
  const g = $('gallery');
  g.innerHTML = '';
  state.gallery.forEach((img, idx) => {
    const thumb = document.createElement('div');
    const isSel = state.selected.has(idx);
    thumb.className = 'gallery-thumb' + (isSel ? ' selected' : '');
    thumb.innerHTML = '<img src="' + img.url + '" alt=""><div class="check">' + (isSel ? '✓' : '') + '</div><div class="del" data-idx="' + idx + '">×</div>';
    thumb.querySelector('.del').addEventListener('click', (e) => {
      e.stopPropagation();
      state.gallery.splice(idx, 1);
      state.selected.delete(idx);
      const newSelected = new Set();
      state.selected.forEach(i => { if (i < idx) newSelected.add(i); else if (i > idx) newSelected.add(i - 1); });
      state.selected = newSelected;
      renderGallery();
      updateToolbar();
    });
    thumb.addEventListener('click', (e) => {
      if (e.shiftKey && state.lastClicked !== null) {
        const start = Math.min(state.lastClicked, idx);
        const end = Math.max(state.lastClicked, idx);
        for (let i = start; i <= end; i++) state.selected.add(i);
      } else {
        if (state.selected.has(idx)) state.selected.delete(idx);
        else state.selected.add(idx);
        state.lastClicked = idx;
      }
      renderGallery();
      updateToolbar();
    });
    g.appendChild(thumb);
  });
  $('galleryCard').style.display = state.gallery.length > 0 ? 'block' : 'none';
  updateToolbar();
}

function updateToolbar() {
  const hasSel = state.selected.size > 0;
  $('galleryToolbar').style.display = state.gallery.length > 0 ? 'flex' : 'none';
  $('selectedCount').textContent = state.selected.size + ' selected';
  $('downloadZipBtn').disabled = !hasSel;
  $('deleteSelectedBtn').disabled = !hasSel;
}

$('selectAllBtn').addEventListener('click', () => {
  state.gallery.forEach((_, i) => state.selected.add(i));
  renderGallery();
});
$('deselectAllBtn').addEventListener('click', () => {
  state.selected.clear();
  renderGallery();
});
$('deleteSelectedBtn').addEventListener('click', () => {
  if (!state.selected.size) return;
  const remaining = state.gallery.filter((_, i) => !state.selected.has(i));
  state.gallery = remaining;
  state.selected.clear();
  renderGallery();
  showToast('Deleted selected images', 'ok');
});
$('downloadZipBtn').addEventListener('click', async () => {
  if (!state.selected.size) { showToast('Select images first', 'err'); return; }
  const urls = [];
  state.selected.forEach(i => { if (state.gallery[i]) urls.push(state.gallery[i].url); });
  try {
    const r = await fetch('/api/export-zip', updateFetchOptions({
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls})
    }));
    if (!r.ok) throw new Error('ZIP failed');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'creative-studio-export.zip';
    a.click();
    showToast('ZIP downloaded', 'ok');
  } catch (e) { showToast('Export failed: ' + e.message, 'err'); }
});

async function loadServerGallery() {
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    if (d.sessions) {
      d.sessions.forEach(s => {
        (s.entries || []).forEach(e => {
          if (e.image_url && !state.gallery.find(g => g.url === e.image_url)) {
            state.gallery.push({ url: e.image_url, name: e.note || 'image.png', cost: e.cost || 0, model: e.model || '' });
          }
        });
      });
      renderGallery();
    }
  } catch (e) { console.log('session load failed', e); }
}
loadServerGallery();

// Delegate click for Save-to-gallery + Copy-prompt on output cells
$('outputGrid').addEventListener('click', (e) => {
  const btn = e.target.closest('.add-to-gallery');
  if (btn) {
    const url = btn.dataset.url;
    const name = btn.dataset.name;
    const cost = parseFloat(btn.dataset.cost) || 0;
    const model = btn.dataset.model || '';
    if (!state.gallery.find(g => g.url === url)) {
      state.gallery.push({ url, name, cost, model, ratio: state.aspect });
      renderGallery();
      showToast('Saved to gallery', 'ok');
    } else {
      showToast('Already in gallery', 'err');
    }
    return;
  }
  const copyBtn = e.target.closest('.copy-prompt');
  if (copyBtn) {
    const prompt = decodeURIComponent(copyBtn.dataset.prompt || '');
    if (prompt) {
      navigator.clipboard.writeText(prompt).then(() => showToast('Prompt copied', 'ok')).catch(() => showToast('Copy failed', 'err'));
    }
  }
});

function loadIntoOutput(images) {
  const grid = $('outputGrid');
  grid.innerHTML = '';
  grid.style.display = 'grid';
  grid.className = 'output-grid' + (images.length === 1 ? ' single' : '');
  $('emptyState').style.display = 'none';
  state.outputImages = images.slice();

  images.forEach((img, i) => {
    const cell = document.createElement('div');
    const ratioClass = (img.ratio || state.aspect || '1:1').replace(':', '-');
    cell.className = 'output-cell fade-in ratio-' + ratioClass;
    cell.style.animationDelay = (i * 0.08) + 's';
    cell.innerHTML = buildCellHTML(img);
    grid.appendChild(cell);
  });

  const totalCost = images.reduce((s, img) => s + (img.cost || 0), 0);
  $('outputMeta').textContent = images.length + ' image' + (images.length > 1 ? 's' : '') + ' · $' + totalCost.toFixed(2);
  $('downloadAllBtn').style.display = images.length > 0 ? 'inline-block' : 'none';
}

function appendToOutput(images) {
  const grid = $('outputGrid');
  grid.style.display = 'grid';
  $('emptyState').style.display = 'none';
  images.forEach(img => state.outputImages.push(img));

  images.forEach((img, i) => {
    const cell = document.createElement('div');
    const ratioClass = (img.ratio || state.aspect || '1:1').replace(':', '-');
    cell.className = 'output-cell fade-in ratio-' + ratioClass;
    cell.style.animationDelay = (i * 0.08) + 's';
    cell.innerHTML = buildCellHTML(img);
    grid.appendChild(cell);
  });

  const allCells = grid.querySelectorAll('.output-cell');
  const totalCost = images.reduce((s, img) => s + (img.cost || 0), 0);
  $('outputMeta').textContent = allCells.length + ' images · streaming...';
  $('downloadAllBtn').style.display = 'inline-block';
}

function dimBadge(ratio) {
  const map = { '1:1': '1024×1024', '4:3': '1024×768', '16:9': '1024×576', '9:16': '576×1024', '2:3': '683×1024', '4:5': '819×1024' };
  return map[ratio] || '';
}

function buildCellHTML(img) {
  const ratio = img.ratio || '';
  const cost = img.cost ? '$' + img.cost.toFixed(2) : '';
  const model = img.model ? img.model.replace('gemini-3.1-flash-image-preview', 'Flash').replace('gemini-3-pro-image-preview', 'Pro') : '';
  const prompt = img.prompt || state.lastPrompt || '';
  const dims = ratio ? dimBadge(ratio) : '';
  return (
    '<img src="' + img.url + '" alt="" data-prompt="' + encodeURIComponent(prompt) + '">' +
    '<div class="cell-bar">' +
      '<div class="left">' +
        (ratio ? '<span class="pill ratio">' + ratio + '</span>' : '') +
        (cost ? '<span class="pill cost">' + cost + '</span>' : '') +
        (model ? '<span class="pill model">' + model + '</span>' : '') +
        (dims ? '<span class="pill dims">' + dims + '</span>' : '') +
      '</div>' +
      '<div class="right">' +
        '<span class="copy-prompt" data-prompt="' + encodeURIComponent(prompt) + '" title="Copy prompt">📋</span>' +
        '<a href="' + img.url + '" download="' + img.name + '">Download</a>' +
        '<span class="add-to-gallery" data-url="' + img.url + '" data-name="' + img.name + '" data-cost="' + (img.cost||0) + '" data-model="' + (img.model||'') + '">Save</span>' +
      '</div>' +
    '</div>'
  );
}

// ── Generate ──
$('genBtn').addEventListener('click', async () => {
  const prompt = $('prompt').value.trim();
  if (!prompt) { showToast('Enter a scene description', 'err'); return; }
  if (state.generating) return;
  state.lastPrompt = prompt;

  const limit = parseFloat($('costLimit').value) || 5;
  if (limit < 0 || isNaN(limit)) { showToast('Invalid cost limit', 'err'); return; }
  const costToday = await getCostToday();
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  const est = state.prodImage ? 0.09 : (({ fast: 0.02, balanced: 0.045, quality: 0.09, ultra: 0.24 })[state.tier] || 0.045) * count;
  if (costToday + est > limit) {
    showToast('Would exceed $' + limit.toFixed(2) + ' cost limit', 'err');
    return;
  }

  state.generating = true;
  $('genBtn').disabled = true;
  $('genBtn').classList.add('generating');
  $('outputMeta').textContent = '';

  // Show skeleton placeholders
  const grid = $('outputGrid');
  grid.innerHTML = '';
  grid.style.display = 'grid';
  grid.className = 'output-grid' + (count === 1 ? ' single' : '');
  $('emptyState').style.display = 'none';
  for (let i = 0; i < count; i++) {
    const sk = document.createElement('div');
    sk.className = 'skeleton-cell ratio-' + (state.aspect || '1:1').replace(':', '-');
    grid.appendChild(sk);
  }

  try {
    let data;
    if (state.prodImage) {
      const fd = new FormData();
      fd.append('prompt', prompt);
      fd.append('product', state.prodImage);
      fd.append('aspect_ratio', state.aspect);
      fd.append('tier', state.tier);
      const resp = await fetch('/api/composite', updateFetchOptions({ method: 'POST', body: fd }));
      data = await resp.json();
    } else {
      const body = {
        prompt, mode: 'direct', tier: state.tier,
        aspect_ratio: state.aspect, variations: count
      };
      const resp = await fetch('/api/generate', updateFetchOptions({
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }));
      data = await resp.json();
    }

    if (data.error) {
      showToast(data.error, 'err');
      return;
    }

    let streamed = false;
    if (data.job_id && data.status === 'running') {
      showToast('Batch started — this takes ~2 minutes', 'ok');
      const result = await pollJob(data.job_id, count);
      if (result.error) { showToast(result.error, 'err'); return; }
      data = result;
      streamed = true;
    }

    if (data.images && data.images.length) {
      if (!streamed) {
        loadIntoOutput(data.images);
        addToGallery(data.images);
      } else if (data.partial) {
        const grid3 = $('outputGrid');
        const retry = document.createElement('div');
        retry.className = 'output-cell';
        retry.style.display = 'flex'; retry.style.alignItems = 'center'; retry.style.justifyContent = 'center';
        retry.style.flexDirection = 'column'; retry.style.gap = '8px'; retry.style.color = 'var(--text-dim)';
        retry.innerHTML = '<div style="font-size:0.85rem;font-weight:600;">' + data.got + '/' + data.expected + ' generated</div><button id="retryBtn" style="padding:6px 14px;border-radius:var(--radius-xs);border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font);font-size:0.78rem;cursor:pointer;">Retry missing</button>';
        grid3.appendChild(retry);
        $('retryBtn').addEventListener('click', () => {
          $('genBtn').click();
        });
        showToast(data.message, 'ok');
        $('downloadAllBtn').style.display = 'inline-block';
      } else {
        showToast(data.message || 'Done!', 'ok');
        $('downloadAllBtn').style.display = 'inline-block';
      }
      refreshCost();
    } else {
      showToast('No images returned', 'err');
    }
  } catch (e) {
    showToast('Network error: ' + e.message, 'err');
  } finally {
    state.generating = false;
    $('genBtn').disabled = false;
    $('genBtn').classList.remove('generating');
  }
});

async function pollJob(jobId, expectedCount) {
  const maxWait = 300;
  const interval = 4;
  const start = Date.now();
  let dots = 0;
  let streamedCount = 0;

  while (true) {
    const elapsed = (Date.now() - start) / 1000;
    if (elapsed > maxWait) {
      return { error: 'Timed out waiting for batch generation' };
    }
    dots = (dots + 1) % 4;
    $('genLabel').textContent = 'Generating' + '.'.repeat(dots) + ' (' + Math.round(elapsed) + 's)';

    await new Promise(r => setTimeout(r, interval * 1000));
    const r = await fetch('/api/jobs/' + jobId);
    const d = await r.json();

    // Stream partial results as they arrive
    if (d.partial && d.partial.images && d.partial.images.length > streamedCount) {
      const newImages = d.partial.images.slice(streamedCount);
      streamedCount = d.partial.images.length;
      // Remove skeleton placeholders, then append real images
      const grid2 = $('outputGrid');
      const skeletons = grid2.querySelectorAll('.skeleton-cell');
      skeletons.forEach((sk, idx) => {
        if (idx < streamedCount) sk.remove();
      });
      appendToOutput(newImages);
      addToGallery(newImages);
      $('genLabel').textContent = 'Generating ' + streamedCount + '/' + expectedCount + '...';
    }

    if (d.status === 'done') {
      $('genLabel').textContent = state.prodImage ? 'Generate Composite' : (expectedCount > 1 ? 'Generate 4 Images' : 'Generate Image');
      $('outputGrid').querySelectorAll('.skeleton-cell').forEach(sk => sk.remove());
      const got = d.images ? d.images.length : 0;
      if (got < expectedCount) {
        return { images: d.images || [], partial: true, expected: expectedCount, got, message: d.message || 'Partial: ' + got + '/' + expectedCount, session_id: d.session_id };
      }
      return { images: d.images || [], message: d.message || 'Done!', session_id: d.session_id };
    }
    if (d.status === 'error') {
      $('genLabel').textContent = state.prodImage ? 'Generate Composite' : (expectedCount > 1 ? 'Generate 4 Images' : 'Generate Image');
      return { error: d.error || 'Generation failed' };
    }
  }
}

$('clearGallery').addEventListener('click', () => {
  state.gallery = [];
  renderGallery();
});

async function refreshCost() {
  try {
    const r = await fetch('/api/costs', updateFetchOptions());
    const d = await r.json();
    $('costToday').textContent = '$' + (d.today?.toFixed(2) || '0.00');
  } catch (e) { console.log('cost fetch failed', e); }
}
refreshCost();

function showToast(msg, type) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── Lightbox ──
const lightbox = {
  overlay: null, img: null, meta: null, list: [], idx: 0,
  init() {
    this.overlay = document.createElement('div');
    this.overlay.className = 'lightbox-overlay';
    this.overlay.innerHTML = (
      '<div class="lightbox-inner">' +
        '<button class="lightbox-close">×</button>' +
        '<img class="lightbox-img" src="" alt="">' +
        '<div class="lightbox-meta"></div>' +
        '<button class="lightbox-nav prev">‹</button>' +
        '<button class="lightbox-nav next">›</button>' +
      '</div>'
    );
    document.body.appendChild(this.overlay);
    this.img = this.overlay.querySelector('.lightbox-img');
    this.meta = this.overlay.querySelector('.lightbox-meta');
    this.overlay.querySelector('.lightbox-close').addEventListener('click', () => this.close());
    this.overlay.querySelector('.lightbox-nav.prev').addEventListener('click', (e) => { e.stopPropagation(); this.prev(); });
    this.overlay.querySelector('.lightbox-nav.next').addEventListener('click', (e) => { e.stopPropagation(); this.next(); });
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
      const copyBtn = e.target.closest('.copy-lightbox');
      if (copyBtn) {
        const prompt = decodeURIComponent(copyBtn.dataset.prompt || '');
        if (prompt) {
          navigator.clipboard.writeText(prompt).then(() => showToast('Prompt copied', 'ok')).catch(() => showToast('Copy failed', 'err'));
        }
      }
    });
    document.addEventListener('keydown', (e) => {
      if (!this.overlay.classList.contains('active')) return;
      if (e.key === 'Escape') this.close();
      if (e.key === 'ArrowLeft') this.prev();
      if (e.key === 'ArrowRight') this.next();
    });
  },
  open(imgList, startIdx) {
    this.list = imgList;
    this.idx = startIdx || 0;
    this.render();
    this.overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
  },
  close() {
    this.overlay.classList.remove('active');
    document.body.style.overflow = '';
  },
  render() {
    const img = this.list[this.idx];
    if (!img) return;
    this.img.src = img.url;
    const ratio = img.ratio || '';
    const cost = img.cost ? '$' + img.cost.toFixed(2) : '';
    const model = img.model ? img.model.replace('gemini-3.1-flash-image-preview', 'Flash').replace('gemini-3-pro-image-preview', 'Pro') : '';
    const prompt = img.prompt || state.lastPrompt || '';
    this.meta.innerHTML = (
      (ratio ? '<span class="pill ratio">' + ratio + '</span>' : '') +
      (cost ? '<span class="pill cost">' + cost + '</span>' : '') +
      (model ? '<span class="pill model">' + model + '</span>' : '') +
      (prompt ? '<span class="copy-lightbox" data-prompt="' + encodeURIComponent(prompt) + '" style="cursor:pointer;margin-left:6px;padding:4px 10px;border-radius:var(--radius-xs);background:rgba(255,255,255,0.08);color:#fff;font-size:0.72rem;"">📋 Copy prompt</span>' : '') +
      '<a href="' + img.url + '" download="' + img.name + '" style="margin-left:8px;padding:4px 10px;border-radius:var(--radius-xs);background:rgba(255,255,255,0.1);color:#fff;font-size:0.72rem;text-decoration:none;">Download</a>'
    );
  },
  prev() { if (this.idx > 0) { this.idx--; this.render(); } },
  next() { if (this.idx < this.list.length - 1) { this.idx++; this.render(); } }
};
lightbox.init();

// Wire lightbox clicks on output grid + gallery
function wireLightbox(container, getImgList) {
  container.addEventListener('click', (e) => {
    const img = e.target.closest('img');
    if (!img) return;
    const list = getImgList();
    const idx = list.findIndex(i => i.url === img.src);
    if (idx !== -1) lightbox.open(list, idx);
  });
}
wireLightbox($('outputGrid'), () => state.outputImages);
wireLightbox($('gallery'), () => state.gallery);

// ── Download all from output stage ──
$('downloadAllBtn').addEventListener('click', async () => {
  if (!state.outputImages.length) { showToast('No images to download', 'err'); return; }
  const urls = state.outputImages.map(img => img.url);
  try {
    const r = await fetch('/api/export-zip', updateFetchOptions({
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls})
    }));
    if (!r.ok) throw new Error('ZIP failed');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'creative-studio-output.zip';
    a.click();
    showToast('ZIP downloaded', 'ok');
  } catch (e) { showToast('Export failed: ' + e.message, 'err'); }
});

// ── Keyboard shortcuts ──
$('prompt').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    $('genBtn').click();
  }
});

// ── Prompt history from server sessions ──
async function loadPromptHistory() {
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    const prompts = [];
    const seen = new Set();
    (d.sessions || []).forEach(s => {
      (s.entries || []).forEach(e => {
        const p = e.prompt || '';
        if (p && !seen.has(p)) { seen.add(p); prompts.push({ text: p, date: s.created_at || '' }); }
      });
    });
    const container = $('promptHistory');
    if (prompts.length) {
      $('promptHistoryPanel').style.display = 'block';
      container.innerHTML = prompts.slice(0, 10).map(p =>
        '<div class="prompt-history-item" data-prompt="' + encodeURIComponent(p.text) + '">' +
          '<span class="prompt-text">' + p.text + '</span>' +
          '<span class="prompt-meta">' + (p.date ? p.date.split('T')[0] : '') + '</span>' +
        '</div>'
      ).join('');
      container.querySelectorAll('.prompt-history-item').forEach(el => {
        el.addEventListener('click', () => {
          $('prompt').value = decodeURIComponent(el.dataset.prompt);
          $('prompt').focus();
        });
      });
    } else {
      $('promptHistoryPanel').style.display = 'none';
    }
  } catch (e) { console.log('prompt history load failed', e); }
}
loadPromptHistory();
