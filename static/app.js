// ════════════════════════════════════════════════════════════════════
// Creative Studio — Refactored editor logic
// ════════════════════════════════════════════════════════════════════

const $ = id => document.getElementById(id);

const state = {
  tier: 'balanced',
  aspect: '1:1',
  prodImage: null,
  productDataUrl: null,  // data:URL/objectURL of uploaded product (for bento source tile)
  generating: false,
  gallery: [],
  selected: new Set(),
  outputImages: [],
  lastPrompt: '',
  apiKey: '',
  costToday: 0,
};

// ── API Key (BYOK) ──────────────────────────────────────────────
const API_KEY_STORAGE = 'cs_api_key';
const loadApiKey = () => localStorage.getItem(API_KEY_STORAGE) || '';
const saveApiKey = (key) => localStorage.setItem(API_KEY_STORAGE, key);

function updateFetchOptions(opts = {}) {
  const key = loadApiKey();
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

const apikeyDot = $('apikeyDot');
const apikeyStatus = $('apikeyStatus');
const apikeyForm = $('apikeyForm');
const apikeyInput = $('apikeyInput');
const apikeyBtn = $('apikeyBtn');
const apikeyEdit = $('apikeyEdit');

function setKeyState(state_) {
  // state_: 'none' | 'shared' | 'user' | 'required'
  apikeyDot.className = 'apikey-status-dot dot-' + state_;
  apikeyEdit.textContent = state_ === 'user' ? 'Change' : 'Add';
  if (state_ === 'user') apikeyStatus.textContent = 'Your Gemini key';
  else if (state_ === 'shared') apikeyStatus.textContent = 'Shared demo key';
  else if (state_ === 'required') apikeyStatus.textContent = 'API key required';
  else apikeyStatus.textContent = 'No key configured';
}

apikeyEdit.addEventListener('click', () => {
  apikeyForm.hidden = false;
  apikeyInput.value = loadApiKey();
  apikeyInput.focus();
  apikeyEdit.textContent = 'Cancel';
  apikeyEdit.onclick = closeApikeyForm;
});

function closeApikeyForm() {
  apikeyForm.hidden = true;
  apikeyEdit.textContent = state.apiKey ? 'Change' : 'Add';
  apikeyEdit.onclick = null;
  apikeyEdit.addEventListener('click', openApikeyForm);
}

function openApikeyForm() {
  apikeyForm.hidden = false;
  apikeyInput.value = loadApiKey();
  apikeyInput.focus();
  apikeyEdit.textContent = 'Cancel';
}

// Replace the inline onclick pattern
apikeyEdit.addEventListener('click', openApikeyForm);

apikeyBtn.addEventListener('click', async () => {
  const key = apikeyInput.value.trim();
  if (!key) { showToast('Paste a key first', 'err'); return; }
  apikeyBtn.disabled = true;
  apikeyBtn.textContent = 'Checking…';
  const result = await validateApiKey(key);
  apikeyBtn.disabled = false;
  apikeyBtn.textContent = 'Save';
  if (result.valid) {
    saveApiKey(key);
    state.apiKey = key;
    setKeyState('user');
    closeApikeyForm();
    showToast('API key saved', 'ok');
  } else {
    showToast(result.error || 'Invalid key', 'err');
  }
});

// Probe whoami to know if there's a server fallback
async function initKeyState() {
  state.apiKey = loadApiKey();
  if (state.apiKey) {
    setKeyState('user');
    return;
  }
  try {
    const r = await fetch('/api/whoami', updateFetchOptions());
    const info = await r.json();
    if (info.fallback_enabled) setKeyState('shared');
    else setKeyState('required');
  } catch (e) { setKeyState('required'); }
}
initKeyState();

// ── Templates (WS-3) — fetch + render + click-to-apply ────────────────
// Each template is a curated preset that fills in (prompt, preset,
// aspect, tier). Fetches /api/templates on boot, renders as clickable
// cards in #templatesRow. Click → applies the template to the form.
async function loadTemplates() {
  const row = document.getElementById('templatesRow');
  if (!row) return;
  try {
    const r = await fetch('/api/templates');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const list = data.templates || [];
    if (!list.length) { row.innerHTML = '<span class="templates-empty">No templates yet</span>'; return; }
    // Group by category so the strip shows section headers
    const byCat = {};
    list.forEach(t => { (byCat[t.category] = byCat[t.category] || []).push(t); });
    row.innerHTML = Object.keys(byCat).sort().map(cat =>
      '<div class="templates-group"><div class="templates-group-label">' + escapeHtml(cat) + '</div>' +
      byCat[cat].map(t =>
        '<button class="template-card" data-template-id="' + escapeHtml(t.id) + '" type="button" ' +
        'title="' + escapeHtml(t.use_case || t.name) + '">' +
        '<div class="template-card-name">' + escapeHtml(t.name) + '</div>' +
        '<div class="template-card-meta">' + escapeHtml(t.aspect) + ' · ' + escapeHtml(t.tier) + ' · ' + escapeHtml(t.preset) + '</div>' +
        '</button>'
      ).join('') + '</div>'
    ).join('');
    // Wire up clicks
    row.querySelectorAll('.template-card').forEach(card => {
      card.addEventListener('click', () => {
        const t = list.find(x => x.id === card.dataset.templateId);
        if (t) applyTemplate(t);
      });
    });
  } catch (e) {
    row.innerHTML = '<span class="templates-empty">Couldn\'t load templates</span>';
  }
}
function applyTemplate(t) {
  // Fill the prompt + select the preset / aspect / tier chips
  const promptEl = document.getElementById('prompt');
  if (promptEl) promptEl.value = t.prompt || '';
  // Highlight the matching preset chip
  document.querySelectorAll('#presetRow .chip').forEach(c => {
    c.classList.toggle('active', c.dataset.preset === t.preset);
  });
  state.preset = t.preset;
  // Aspect
  document.querySelectorAll('#aspectRow .chip').forEach(c => {
    c.classList.toggle('active', c.dataset.ratio === t.aspect);
  });
  state.aspect = t.aspect;
  // Tier (quality)
  document.querySelectorAll('#qualityRow .chip').forEach(c => {
    c.classList.toggle('active', c.dataset.tier === t.tier);
  });
  state.tier = t.tier;
  if (typeof updateGenLabel === 'function') updateGenLabel();
  if (typeof showToast === 'function') showToast('Loaded: ' + t.name, 'ok');
}
loadTemplates();

// ── Projects (WS-4) — fetch + render + click-to-open ───────────────────
// Signed-in users get a "My projects" sidebar. Each project is a
// clickable card that opens the project (loads generations into
// the gallery). The "+ New" button creates an empty project and
// immediately opens it.
async function loadProjects() {
  const panel = document.getElementById('projectsPanel');
  const list = document.getElementById('projectsList');
  if (!panel || !list) return;
  // Only show for signed-in users. /api/me is the source of truth.
  try {
    const r = await fetch('/api/me');
    if (r.status === 401) { panel.hidden = true; return; }
  } catch (e) { return; }
  panel.hidden = false;
  await refreshProjectsList();
}
async function refreshProjectsList() {
  const list = document.getElementById('projectsList');
  if (!list) return;
  try {
    const r = await fetch('/api/projects');
    if (!r.ok) { list.innerHTML = '<span class="projects-empty">Couldn\'t load projects</span>'; return; }
    const data = await r.json();
    const projects = data.projects || [];
    if (!projects.length) { list.innerHTML = '<span class="projects-empty">No projects yet — click + New</span>'; return; }
    list.innerHTML = projects.map(p =>
      '<div class="project-card" data-project-id="' + escapeHtml(p.id) + '" title="Created ' + escapeHtml(p.created_at || '') + '">' +
        (p.hero_url ? '<div class="project-card-hero"><img src="' + escapeHtml(p.hero_url) + '" alt="" loading="lazy"></div>' : '<div class="project-card-hero project-card-hero-empty">No image yet</div>') +
        '<div class="project-card-name">' + escapeHtml(p.name || 'Untitled') + '</div>' +
      '</div>'
    ).join('');
    list.querySelectorAll('.project-card').forEach(card => {
      card.addEventListener('click', () => {
        const pid = card.dataset.projectId;
        if (pid) openProject(pid);
      });
    });
  } catch (e) {
    list.innerHTML = '<span class="projects-empty">Couldn\'t load projects</span>';
  }
}
async function openProject(projectId) {
  // For v1: download the export zip and stash it in localStorage.
  // For v2: load generations into the gallery. The zip is the
  // canonical artifact for now (small enough to be a useful product
  // on its own).
  try {
    const r = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/export');
    if (!r.ok) { if (typeof showToast === 'function') showToast('Couldn\'t open project', 'err'); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'project-' + projectId.slice(0, 8) + '.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    if (typeof showToast === 'function') showToast('Downloaded project zip', 'ok');
  } catch (e) {
    if (typeof showToast === 'function') showToast('Error opening project', 'err');
  }
}
async function createNewProject() {
  const name = prompt('Project name?', 'Untitled project');
  if (!name) return;
  const r = await fetch('/api/projects', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name}),
  });
  if (!r.ok) { if (typeof showToast === 'function') showToast('Couldn\'t create project', 'err'); return; }
  const proj = await r.json();
  await refreshProjectsList();
  if (typeof showToast === 'function') showToast('Created: ' + proj.name, 'ok');
}
loadProjects();
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('newProjectBtn');
  if (btn) btn.addEventListener('click', createNewProject);
});

// ── Library (WS-7) — every generation the user has ever made ──────────────────
// The Library tab shows the user's full history. Search by prompt
// text, filter by aspect ratio, paginated. Click an item to drop
// it into the gallery (for re-use) or open the full-size version.
let libraryOffset = 0;
const LIBRARY_PAGE = 60;
async function loadLibrary() {
  const panel = document.getElementById('libraryPanel');
  const grid = document.getElementById('libraryGrid');
  const hint = document.getElementById('libraryHint');
  const footer = document.getElementById('libraryFooter');
  if (!panel || !grid) return;
  // Only show for signed-in users
  try {
    const r = await fetch('/api/me');
    if (r.status === 401) { panel.hidden = true; return; }
  } catch (e) { return; }
  panel.hidden = false;
  await refreshLibrary();
}
async function refreshLibrary() {
  const grid = document.getElementById('libraryGrid');
  const hint = document.getElementById('libraryHint');
  const footer = document.getElementById('libraryFooter');
  const search = (document.getElementById('librarySearch') || {}).value || '';
  const aspect = (document.getElementById('libraryAspectFilter') || {}).value || '';
  const params = new URLSearchParams({limit: LIBRARY_PAGE, offset: libraryOffset});
  if (search) params.set('search', search);
  if (aspect) params.set('aspect', aspect);
  try {
    const r = await fetch('/api/library?' + params.toString());
    if (!r.ok) { grid.innerHTML = '<span class="library-empty">Couldn\'t load library</span>'; return; }
    const data = await r.json();
    const items = data.items || [];
    if (libraryOffset === 0) {
      grid.innerHTML = '';
    }
    if (items.length === 0 && libraryOffset === 0) {
      grid.innerHTML = '<span class="library-empty">No generations yet — make your first one in the editor</span>';
    } else {
      const frag = document.createDocumentFragment();
      items.forEach(it => frag.appendChild(buildLibraryCard(it)));
      grid.appendChild(frag);
    }
    hint.textContent = data.total ? (data.total + ' image' + (data.total === 1 ? '' : 's')) : '';
    footer.innerHTML = (libraryOffset + items.length) < data.total
      ? '<button class="link-btn" id="libraryMoreBtn" type="button">Load more</button>'
      : '';
    const moreBtn = document.getElementById('libraryMoreBtn');
    if (moreBtn) moreBtn.onclick = () => { libraryOffset += LIBRARY_PAGE; refreshLibrary(); };
  } catch (e) {
    grid.innerHTML = '<span class="library-empty">Couldn\'t load library</span>';
  }
}
function buildLibraryCard(it) {
  const card = document.createElement('div');
  card.className = 'library-card';
  card.innerHTML =
    '<div class="library-card-img"><img src="' + escapeHtml(it.url) + '" alt="" loading="lazy"></div>' +
    '<div class="library-card-meta">' +
      '<span class="library-card-name">' + escapeHtml(it.name) + '</span>' +
      (it.aspect ? '<span class="library-card-aspect">' + escapeHtml(it.aspect) + '</span>' : '') +
      (it.prompt ? '<span class="library-card-prompt" title="' + escapeHtml(it.prompt) + '">' + escapeHtml(it.prompt.slice(0, 80)) + (it.prompt.length > 80 ? '…' : '') + '</span>' : '') +
    '</div>' +
    '<div class="library-card-actions">' +
      '<button class="link-btn" data-action="library-load" aria-label="Use in editor">Use</button>' +
      '<a class="link-btn" href="' + escapeHtml(it.url) + '" download aria-label="Download">Download</a>' +
      '<button class="link-btn link-btn-danger" data-action="library-delete" aria-label="Delete">×</button>' +
    '</div>';
  card.querySelector('[data-action="library-load"]').onclick = () => {
    // Drop into the gallery for the current session
    if (!state.gallery.find(g => g.url === it.url)) {
      state.gallery.push({ url: it.url, name: it.name, cost: 0, model: '', ratio: it.aspect || state.aspect });
      renderGallery();
      showToast('Added to gallery', 'ok');
    } else {
      showToast('Already in gallery', 'ok');
    }
  };
  card.querySelector('[data-action="library-delete"]').onclick = async () => {
    if (!confirm('Delete this image from the library? (This is permanent.)')) return;
    const r = await fetch('/api/library/' + encodeURIComponent(it.path) + '/delete',
      { method: 'POST', headers: { 'X-Session-Token': localStorage.getItem('photogen_session') || '' } });
    if (r.ok) { showToast('Deleted', 'ok'); libraryOffset = 0; refreshLibrary(); }
    else { showToast('Delete failed', 'err'); }
  };
  return card;
}
loadLibrary();
// Library search/filter (debounced)
let librarySearchTimer = null;
document.addEventListener('DOMContentLoaded', () => {
  const search = document.getElementById('librarySearch');
  const aspect = document.getElementById('libraryAspectFilter');
  if (search) {
    search.addEventListener('input', () => {
      clearTimeout(librarySearchTimer);
      librarySearchTimer = setTimeout(() => { libraryOffset = 0; refreshLibrary(); }, 300);
    });
  }
  if (aspect) {
    aspect.addEventListener('change', () => { libraryOffset = 0; refreshLibrary(); });
  }
});

// ── Scene types (work for any product) ────────────────────────────
// Each scene type has a prompt template that describes the *scene* but leaves
// the product generic ("the product"). When the user uploads a product photo,
// it's composited in via the existing Product Compositing flow.
const SCENE_TYPES = {
  inhand: {
    label: 'In-hand',
    prompt: 'Close-up of a hand holding the product, natural skin tone, soft daylight from window, shallow depth of field, the hand fills the lower half of the frame, product in sharp focus, editorial product photography, 85mm lens',
    aspect: '4:5',
  },
  studio: {
    label: 'Studio',
    prompt: 'Product on a clean seamless studio backdrop, controlled soft-box lighting from upper left, soft natural shadow underneath, perfectly centered, no distractions, ecommerce-grade product photography, color-calibrated white background, sharp from edge to edge',
    aspect: '1:1',
  },
  action: {
    label: 'Action',
    prompt: 'Product in mid-use, dynamic action moment — pouring, opening, applying, or being squeezed — motion implied by blur on liquid or cap, frozen peak moment, high shutter speed feel, dramatic side lighting, lifestyle energy, candid and authentic',
    aspect: '4:5',
  },
  lifestyle: {
    label: 'Lifestyle',
    prompt: 'Product in a real-world lifestyle scene with a person, natural environment (cafe, kitchen, gym, park, or shelf), warm available light, authentic and unstaged feeling, the person is mid-activity, product naturally placed, shot in documentary style, human warmth',
    aspect: '4:5',
  },
  withprops: {
    label: 'With props',
    prompt: 'Product styled with complementary props that suggest its category and use — fresh ingredients, accessories, tools, or pairing items — arranged on a textured surface (marble, wood, linen), overhead 45 degree angle, editorial flatlay composition, warm natural light, the product is the focal point with props supporting',
    aspect: '1:1',
  },
};

document.querySelectorAll('#presetRow .chip-scene').forEach(chip => {
  chip.addEventListener('click', () => {
    const scene = SCENE_TYPES[chip.dataset.preset];
    if (!scene) return;
    $('prompt').value = scene.prompt;
    state.aspect = scene.aspect;
    document.querySelectorAll('#aspectRow .chip').forEach(c => {
      c.classList.toggle('active', c.dataset.ratio === scene.aspect);
    });
    document.querySelectorAll('#presetRow .chip-scene').forEach(c => c.classList.toggle('active', c === chip));
  });
});

// Read ?preset=&ratio=&prompt= from the landing page gallery cards.
// Runs once on init. Overrides the default preset + prompt when present.
function applyUrlParams() {
  const p = new URLSearchParams(window.location.search);
  if (![...p.keys()].length) return;

  const preset = p.get('preset');
  const ratio = p.get('ratio');
  const promptText = p.get('prompt');

  if (preset && SCENE_TYPES[preset]) {
    const scene = SCENE_TYPES[preset];
    if (promptText) $('prompt').value = promptText;
    else $('prompt').value = scene.prompt;
    document.querySelectorAll('#presetRow .chip-scene').forEach(c => c.classList.toggle('active', c.dataset.preset === preset));
    if (ratio) {
      state.aspect = ratio;
      document.querySelectorAll('#aspectRow .chip').forEach(c => c.classList.toggle('active', c.dataset.ratio === ratio));
    } else {
      state.aspect = scene.aspect;
      document.querySelectorAll('#aspectRow .chip').forEach(c => c.classList.toggle('active', c.dataset.ratio === scene.aspect));
    }
    updateGenLabel();
  } else if (promptText) {
    $('prompt').value = promptText;
    if (ratio) {
      state.aspect = ratio;
      document.querySelectorAll('#aspectRow .chip').forEach(c => c.classList.toggle('active', c.dataset.ratio === ratio));
      updateGenLabel();
    }
  }
}
// Invocation deferred to end of file so TIER_COST / updateGenLabel are initialized.

// ── Chip selectors (aspect + quality) ───────────────────────────
function bindChips(rowId, onChange) {
  $(rowId).addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    document.querySelectorAll('#' + rowId + ' .chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    onChange(chip);
  });
}
bindChips('aspectRow', chip => { state.aspect = chip.dataset.ratio; updateGenLabel(); });
bindChips('qualityRow', chip => { state.tier = chip.dataset.tier; updateGenLabel(); updateSceneSetLabel(); });

// ── Dropzone ─────────────────────────────────────────────────────
const dropzone = $('dropzone');
const fileInput = $('fileInput');
const dropzoneEmpty = $('dropzoneEmpty');
const dropzoneFilled = $('dropzoneFilled');
const previewImg = $('previewImg');
const removeBtn = $('removeBtn');

function onFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    showToast('That doesn\'t look like an image', 'err');
    return;
  }
  if (file.size > 32 * 1024 * 1024) {
    showToast('Image too large (32MB max)', 'err');
    return;
  }
  state.prodImage = file;
  const url = URL.createObjectURL(file);
  state.productDataUrl = url;  // keep a reference for the output bento source tile
  previewImg.src = url;
  dropzoneEmpty.hidden = true;
  dropzoneFilled.hidden = false;
  // Reveal the "Generate all 5 scenes" button — only useful with a product
  $('sceneSetBtn').hidden = false;
  updateGenLabel();
}

dropzone.addEventListener('click', e => {
  // Don't double-trigger from remove button
  if (e.target.closest('.dropzone-remove')) return;
  fileInput.click();
});
dropzone.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener('change', e => onFile(e.target.files[0]));

dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', e => {
  e.preventDefault(); dropzone.classList.remove('dragover');
  onFile(e.dataTransfer.files[0]);
});

removeBtn.addEventListener('click', e => {
  e.stopPropagation();
  state.prodImage = null;
  fileInput.value = '';
  previewImg.src = '';
  dropzoneEmpty.hidden = false;
  dropzoneFilled.hidden = true;
  updateGenLabel();
});

// ── Generate label ──────────────────────────────────────────────
const TIER_COST = { fast: 0.02, balanced: 0.045, quality: 0.09, ultra: 0.24 };

function updateGenLabel() {
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  let label = 'Generate';
  if (state.prodImage) label = 'Generate composite';
  else if (batch) label = 'Generate 4 images';
  else label = 'Generate image';
  $('genLabel').textContent = label;

  const unit = TIER_COST[state.tier] || 0.045;
  const cost = state.prodImage ? TIER_COST.quality : unit * count;
  const time = batch && !state.prodImage ? '~2 min' : '~30s';
  $('genMeta').textContent = '· $' + cost.toFixed(2) + ' · ' + time;
}
$('batchToggle').addEventListener('change', updateGenLabel);
updateGenLabel();

// ── Cost ─────────────────────────────────────────────────────────
async function refreshCost() {
  try {
    const r = await fetch('/api/costs', updateFetchOptions());
    const d = await r.json();
    state.costToday = d.today || 0;
  } catch (e) { /* offline */ }
}
refreshCost();

// ── Output rendering ────────────────────────────────────────────
const outputEmpty = $('outputEmpty');
const outputGrid = $('outputGrid');
const exampleChips = document.querySelectorAll('.example-chip');

exampleChips.forEach(chip => {
  chip.addEventListener('click', () => {
    $('prompt').value = chip.textContent.replace(/^["']|["']$/g, '');
  });
});

function ratioClass(r) { return 'ratio-' + (r || '1:1').replace(':', '-'); }

function showEmpty() {
  outputEmpty.hidden = false;
  outputGrid.hidden = true;
  outputGrid.innerHTML = '';
}

function showOutput(images, append = false) {
  outputEmpty.hidden = true;
  outputGrid.hidden = false;
  if (!append) outputGrid.innerHTML = '';

  // Prepend source product tile if user uploaded one (anchors the bento)
  const totalCells = (state.productDataUrl ? 1 : 0) + images.length;
  if (!append && state.productDataUrl) {
    const src = document.createElement('div');
    src.className = 'output-cell is-source';
    src.innerHTML = `
      <img src="${state.productDataUrl}" alt="Source product">
      <span class="source-badge">Source</span>
    `;
    outputGrid.appendChild(src);
  }

  // Bento class based on total cell count
  const countClass = 'count-' + Math.min(Math.max(totalCells, 1), 6);
  outputGrid.className = 'output-grid ' + countClass;

  images.forEach((img, i) => {
    const cell = document.createElement('div');
    cell.className = 'output-cell ' + ratioClass(img.ratio || state.aspect);
    cell.style.animationDelay = (i * 0.06) + 's';
    cell.innerHTML = buildCellHTML(img);
    outputGrid.appendChild(cell);
  });
}

function safeAttr(s) {
  // Escape a value for safe insertion into an HTML attribute (double-quoted).
  // All cells currently use img.url, img.name, img.model, img.prompt, and a
  // numeric cost. img.name flows from f.name on the server, img.prompt is
  // URI-encoded by encodeURIComponent above — but defense-in-depth: never
  // trust any string into innerHTML.
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '&#10;')
    .replace(/\r/g, '&#13;');
}

function buildCellHTML(img) {
  const ratio = img.ratio || '';
  const cost = img.cost ? '$' + img.cost.toFixed(2) : '';
  const model = (img.model || '').replace('gemini-3.1-flash-image-preview', 'Flash').replace('gemini-3-pro-image-preview', 'Pro').replace('imagen-4.0-', '');
  const prompt = encodeURIComponent(img.prompt || state.lastPrompt || '');
  const dims = ratio ? dimBadge(ratio) : '';
  return `
    <img src="${safeAttr(img.url)}" alt="" loading="lazy">
    <div class="cell-overlay">
      <div class="cell-meta">
        ${ratio ? `<span class="cell-tag">${safeAttr(ratio)}</span>` : ''}
        ${dims ? `<span class="cell-tag cell-tag-dim">${safeAttr(dims)}</span>` : ''}
        ${cost ? `<span class="cell-tag cell-tag-cost">${safeAttr(cost)}</span>` : ''}
        ${model ? `<span class="cell-tag cell-tag-model">${safeAttr(model)}</span>` : ''}
      </div>
      <div class="cell-actions">
        <button class="cell-action" data-action="save" data-url="${safeAttr(img.url)}" data-name="${safeAttr(img.name)}" data-cost="${safeAttr(img.cost||0)}" data-model="${safeAttr(img.model||'')}" aria-label="Save to gallery">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
        </button>
        <button class="cell-action" data-action="more-like-this" data-prompt="${safeAttr(encodeURIComponent(img.prompt || state.lastPrompt || ''))}" data-aspect="${safeAttr(img.ratio || state.aspect)}" data-name="${safeAttr(img.name)}" aria-label="More like this (4 variations)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
        </button>
        <button class="cell-action" data-action="copy" data-prompt="${safeAttr(prompt)}" aria-label="Copy prompt">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        </button>
        <a class="cell-action" href="${safeAttr(img.url)}" download="${safeAttr(img.name)}" aria-label="Download">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </a>
      </div>
    </div>`;
}

function dimBadge(ratio) {
  const map = { '1:1': '1024×1024', '4:3': '1024×768', '16:9': '1024×576', '9:16': '576×1024', '2:3': '683×1024', '4:5': '819×1024' };
  return map[ratio] || '';
}

outputGrid.addEventListener('click', e => {
  const save = e.target.closest('[data-action="save"]');
  if (save) {
    const url = save.dataset.url;
    if (!state.gallery.find(g => g.url === url)) {
      state.gallery.push({ url, name: save.dataset.name, cost: parseFloat(save.dataset.cost) || 0, model: save.dataset.model, ratio: state.aspect });
      renderGallery();
      showToast('Saved to gallery', 'ok');
    } else {
      showToast('Already in gallery', 'ok');
    }
    return;
  }
  // "More like this" — re-run the same prompt with 4 variations.
  // Reuses the existing /api/generate `variations` param. The output
  // is dropped into the same output grid so the user can compare.
  const more = e.target.closest('[data-action="more-like-this"]');
  if (more) {
    if (state.generating) { showToast('Already generating', 'err'); return; }
    const prompt = decodeURIComponent(more.dataset.prompt || '');
    if (!prompt) { showToast('No prompt to re-use', 'err'); return; }
    showToast('Generating 4 variations…', 'ok');
    state.generating = true;
    state.lastPrompt = prompt;
    // Switch the aspect chip to match the source image (so the new
    // 4 cells are the right shape).
    const aspect = more.dataset.aspect || state.aspect;
    document.querySelectorAll('#aspectRow .chip').forEach(c => {
      c.classList.toggle('active', c.dataset.ratio === aspect);
    });
    state.aspect = aspect;
    outputEmpty.hidden = true;
    outputGrid.hidden = false;
    outputGrid.innerHTML = '';
    for (let i = 0; i < 4; i++) {
      const sk = document.createElement('div');
      sk.className = 'skeleton-cell ' + ratioClass(aspect);
      outputGrid.appendChild(sk);
    }
    outputGrid.className = 'output-grid count-4';
    fetch('/api/generate', updateFetchOptions({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, mode: 'direct', tier: state.tier, aspect_ratio: aspect, variations: 4 })
    })).then(r => r.json()).then(data => {
      state.generating = false;
      if (data.error) { showToast(data.error, 'err'); return; }
      if (data.job_id && data.status === 'running') {
        return pollJob(data.job_id, 4).then(result => {
          if (result.error) { showToast(result.error, 'err'); return; }
          showOutput(result.images || []);
        });
      }
      showOutput(data.images || []);
    }).catch(e => {
      state.generating = false;
      showToast('Variation request failed: ' + (e.message || e), 'err');
    });
    return;
  }
  const copy = e.target.closest('[data-action="copy"]');
  if (copy) {
    const prompt = decodeURIComponent(copy.dataset.prompt || '');
    if (prompt) {
      navigator.clipboard.writeText(prompt).then(() => showToast('Prompt copied', 'ok')).catch(() => showToast('Copy failed', 'err'));
    }
    return;
  }
  // Lightbox
  const img = e.target.closest('img');
  if (img) {
    const list = state.outputImages;
    const idx = list.findIndex(i => i.url === img.src);
    if (idx !== -1) lightboxOpen(list, idx);
  }
});

// ── Generate ─────────────────────────────────────────────────────
const genBtn = $('genBtn');
const btnSpinner = $('btnSpinner');
const genMeta = $('genMeta');

genBtn.addEventListener('click', async () => {
  const prompt = $('prompt').value.trim();
  if (!prompt) { showToast('Describe a scene first', 'err'); return; }
  if (state.generating) return;
  state.lastPrompt = prompt;
  state.generating = true;
  genBtn.disabled = true;
  genBtn.classList.add('is-loading');
  btnSpinner.hidden = false;
  genMeta.textContent = '';

  // Show skeletons (with source tile as anchor if product uploaded)
  const count = state.prodImage ? 1 : ($('batchToggle').checked ? 4 : 1);
  const totalCount = (state.productDataUrl ? 1 : 0) + count;
  outputEmpty.hidden = true;
  outputGrid.hidden = false;
  outputGrid.innerHTML = '';

  // Prepend source tile (just like the real output)
  if (state.productDataUrl) {
    const src = document.createElement('div');
    src.className = 'output-cell is-source';
    src.innerHTML = `<img src="${state.productDataUrl}" alt="Source product"><span class="source-badge">Source</span>`;
    outputGrid.appendChild(src);
  }

  const countClass = 'count-' + Math.min(Math.max(totalCount, 1), 6);
  outputGrid.className = 'output-grid ' + countClass;
  for (let i = 0; i < count; i++) {
    const sk = document.createElement('div');
    sk.className = 'skeleton-cell ' + ratioClass(state.aspect);
    outputGrid.appendChild(sk);
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
      const resp = await fetch('/api/generate', updateFetchOptions({
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, mode: 'direct', tier: state.tier, aspect_ratio: state.aspect, variations: count })
      }));
      data = await resp.json();
    }

    if (data.error) {
      showOutput([]);
      showEmpty();
      showToast(data.error, 'err');
      return;
    }

    if (data.job_id && data.status === 'running') {
      showToast('Batch started — about 2 minutes', 'ok');
      const result = await pollJob(data.job_id, count);
      if (result.error) { showEmpty(); showToast(result.error, 'err'); return; }
      data = result;
    }

    if (data.images && data.images.length) {
      showOutput(data.images);
      addToGallery(data.images);
      showToast('Done', 'ok');
      refreshCost();
    } else {
      showEmpty();
      showToast('No images returned', 'err');
    }
  } catch (e) {
    showEmpty();
    showToast('Network error: ' + e.message, 'err');
  } finally {
    state.generating = false;
    genBtn.disabled = false;
    genBtn.classList.remove('is-loading');
    btnSpinner.hidden = true;
    updateGenLabel();
  }
});

// ── Scene-set: one product → 5 scene images in parallel ──
const sceneSetBtn = $('sceneSetBtn');
const sceneSetMeta = $('sceneSetMeta');
const SCENE_LABELS_JS = {
  inhand: 'In-hand', studio: 'Studio', action: 'Action',
  lifestyle: 'Lifestyle', withprops: 'With props',
};
const TIER_COST_PER_IMAGE = { fast: 0.02, balanced: 0.045, quality: 0.09, ultra: 0.24 };
const TIER_TIME_PER_IMAGE = { fast: 15, balanced: 30, quality: 30, ultra: 45 };

function updateSceneSetLabel() {
  const tier = state.tier || 'balanced';
  const cost = (TIER_COST_PER_IMAGE[tier] * 5).toFixed(2);
  const sec = TIER_TIME_PER_IMAGE[tier] * 2;  // rough total for 5 parallel
  sceneSetMeta.textContent = `~$${cost} · ~${sec}s`;
}
updateSceneSetLabel();
sceneSetBtn.addEventListener('click', async () => {
  if (!state.prodImage) { showToast('Upload a product first', 'err'); return; }
  if (state.generating) return;
  state.generating = true;
  sceneSetBtn.disabled = true;
  genBtn.disabled = true;  // prevent double-fire
  sceneSetBtn.classList.add('is-loading');
  genBtn.classList.add('is-loading');
  const originalMeta = sceneSetMeta.textContent;
  sceneSetMeta.textContent = 'Generating 5 scenes…';

  // Show 5 skeleton tiles with scene labels, plus the source tile as anchor
  outputEmpty.hidden = true;
  outputGrid.hidden = false;
  outputGrid.innerHTML = '';
  if (state.productDataUrl) {
    const src = document.createElement('div');
    src.className = 'output-cell is-source';
    src.innerHTML = `<img src="${state.productDataUrl}" alt="Source product"><span class="source-badge">Source</span>`;
    outputGrid.appendChild(src);
  }
  // Bento: 6 cells (1 source + 5 scenes) — use the count-6 3-col layout
  outputGrid.className = 'output-grid count-6';
  const SCENES = ['inhand', 'studio', 'action', 'lifestyle', 'withprops'];
  SCENES.forEach((scene, i) => {
    const sk = document.createElement('div');
    sk.className = 'skeleton-cell scene-loading';
    sk.dataset.scene = scene;
    sk.innerHTML = `<span class="skeleton-label">${SCENE_LABELS_JS[scene]}</span>`;
    outputGrid.appendChild(sk);
  });

  try {
    const fd = new FormData();
    fd.append('product', state.prodImage);
    fd.append('tier', state.tier);
    const resp = await fetch('/api/scene-set', updateFetchOptions({ method: 'POST', body: fd }));
    const data = await resp.json();
    if (data.error) {
      showEmpty();
      showToast(data.error, 'err');
      return;
    }

    // Replace skeletons with real images as they come back, in scene order
    const got = data.images || [];
    for (const img of got) {
      const sk = outputGrid.querySelector(`.skeleton-cell[data-scene="${img.scene}"]`);
      if (sk) {
        sk.outerHTML = buildCellHTML(img);
        // Re-apply output-cell class to the inserted node since buildCellHTML
        // produces a <div class="output-cell ..."> already.
        const newCell = outputGrid.querySelector(`img[alt=""]`);
        if (newCell && newCell.parentElement) {
          newCell.parentElement.className = 'output-cell ' + ratioClass(img.ratio);
        }
      } else {
        // No matching skeleton (shouldn't happen), append at end
        const cell = document.createElement('div');
        cell.className = 'output-cell ' + ratioClass(img.ratio);
        cell.innerHTML = buildCellHTML(img);
        outputGrid.appendChild(cell);
      }
    }

    // Remove any unfilled skeletons
    outputGrid.querySelectorAll('.skeleton-cell').forEach(sk => sk.remove());

    addToGallery(got);
    refreshCost();
    showToast(data.message || `Generated ${got.length} scene(s)`, got.length === 5 ? 'ok' : 'err');
  } catch (e) {
    showEmpty();
    showToast('Network error: ' + e.message, 'err');
  } finally {
    state.generating = false;
    sceneSetBtn.disabled = false;
    genBtn.disabled = false;
    sceneSetBtn.classList.remove('is-loading');
    genBtn.classList.remove('is-loading');
    sceneSetMeta.textContent = originalMeta;
  }
});

async function pollJob(jobId, expected) {
  const start = Date.now();
  let streamed = 0;
  while (true) {
    await new Promise(r => setTimeout(r, 3500));
    const d = await fetch('/api/jobs/' + jobId, updateFetchOptions()).then(r => r.json());
    if (d.partial && d.partial.images && d.partial.images.length > streamed) {
      const newOnes = d.partial.images.slice(streamed);
      streamed = d.partial.images.length;
      // Replace skeletons with real images as they stream
      const sk = outputGrid.querySelectorAll('.skeleton-cell');
      for (let i = 0; i < newOnes.length; i++) {
        if (sk[i]) sk[i].remove();
      }
      newOnes.forEach(img => state.outputImages.push(img));
      addToGallery(newOnes);
      showOutput(state.outputImages, true);
      genMeta.textContent = '· ' + streamed + '/' + expected;
    }
    if (d.status === 'done') return { images: d.images || state.outputImages };
    if (d.status === 'error') return { error: d.error || 'Generation failed' };
    if ((Date.now() - start) / 1000 > 300) return { error: 'Timed out' };
  }
}

// ── Gallery ──────────────────────────────────────────────────────
const galleryCard = $('galleryCard');
const galleryGrid = $('gallery');
const clearGalleryBtn = $('clearGallery');
const selectAllBtn = $('selectAllBtn');
const deselectAllBtn = $('deselectAllBtn');
const downloadZipBtn = $('downloadZipBtn');

function addToGallery(images) {
  images.forEach(img => {
    if (!state.gallery.find(g => g.url === img.url)) {
      state.gallery.push({ url: img.url, name: img.name, cost: img.cost, model: img.model, ratio: img.ratio });
    }
  });
  renderGallery();
}

function renderGallery() {
  galleryGrid.innerHTML = '';
  if (state.gallery.length === 0) {
    galleryCard.hidden = true;
    return;
  }
  galleryCard.hidden = false;
  state.gallery.forEach((img, idx) => {
    const t = document.createElement('div');
    t.className = 'gallery-item' + (state.selected.has(idx) ? ' is-selected' : '');
    t.innerHTML = `<img src="${img.url}" alt="" loading="lazy">
      <button class="gallery-item-del" data-idx="${idx}" aria-label="Remove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>`;
    t.addEventListener('click', e => {
      if (e.target.closest('.gallery-item-del')) return;
      if (state.selected.has(idx)) state.selected.delete(idx);
      else state.selected.add(idx);
      renderGallery();
    });
    galleryGrid.appendChild(t);
  });
  updateGalleryActions();
}

function updateGalleryActions() {
  const has = state.gallery.length > 0;
  const sel = state.selected.size;
  selectAllBtn.hidden = sel > 0;
  deselectAllBtn.hidden = sel === 0;
  downloadZipBtn.disabled = sel === 0;
}

clearGalleryBtn.addEventListener('click', () => {
  if (!state.gallery.length) return;
  if (!confirm('Clear all ' + state.gallery.length + ' images from this session?')) return;
  state.gallery = [];
  state.selected.clear();
  renderGallery();
});

galleryGrid.addEventListener('click', e => {
  const del = e.target.closest('.gallery-item-del');
  if (del) {
    e.stopPropagation();
    state.gallery.splice(parseInt(del.dataset.idx), 1);
    state.selected.clear();
    renderGallery();
  }
});

selectAllBtn.addEventListener('click', () => {
  state.gallery.forEach((_, i) => state.selected.add(i));
  renderGallery();
});

deselectAllBtn.addEventListener('click', () => {
  state.selected.clear();
  renderGallery();
});

downloadZipBtn.addEventListener('click', async () => {
  if (!state.selected.size) return;
  const urls = [];
  state.selected.forEach(i => { if (state.gallery[i]) urls.push(state.gallery[i].url); });
  try {
    const r = await fetch('/api/export-zip', updateFetchOptions({
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ urls })
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

galleryGrid.addEventListener('click', e => {
  const img = e.target.closest('img');
  if (!img) return;
  const idx = state.gallery.findIndex(g => g.url === img.src);
  if (idx !== -1) lightboxOpen(state.gallery, idx);
});

// ── Lightbox ─────────────────────────────────────────────────────
const lightbox = $('lightbox');
const lightboxImg = $('lightboxImg');
let lightboxList = [];
let lightboxIdx = 0;

function lightboxOpen(list, idx) {
  lightboxList = list;
  lightboxIdx = idx;
  lightbox.hidden = false;
  document.body.style.overflow = 'hidden';
  lightboxRender();
}
function lightboxClose() {
  lightbox.hidden = true;
  document.body.style.overflow = '';
}
function lightboxRender() {
  const img = lightboxList[lightboxIdx];
  if (!img) return;
  lightboxImg.src = img.url;
  $('lightboxPrev').disabled = lightboxIdx === 0;
  $('lightboxNext').disabled = lightboxIdx === lightboxList.length - 1;
}
$('lightboxClose').addEventListener('click', lightboxClose);
$('lightboxPrev').addEventListener('click', () => { if (lightboxIdx > 0) { lightboxIdx--; lightboxRender(); } });
$('lightboxNext').addEventListener('click', () => { if (lightboxIdx < lightboxList.length - 1) { lightboxIdx++; lightboxRender(); } });
lightbox.addEventListener('click', e => { if (e.target === lightbox) lightboxClose(); });
document.addEventListener('keydown', e => {
  if (lightbox.hidden) return;
  if (e.key === 'Escape') lightboxClose();
  if (e.key === 'ArrowLeft') $('lightboxPrev').click();
  if (e.key === 'ArrowRight') $('lightboxNext').click();
});

// ── Toast ────────────────────────────────────────────────────────
const toast = $('toast');
let toastTimer = null;
function showToast(msg, type = 'ok') {
  toast.textContent = msg;
  toast.className = 'toast toast-' + type + ' is-visible';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 2800);
}

// ── Mobile menu ──────────────────────────────────────────────────
const menuToggle = $('menuToggle');
const mobileMenu = $('mobileMenu');
menuToggle.addEventListener('click', () => {
  const open = mobileMenu.classList.toggle('open');
  menuToggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
});
mobileMenu.querySelectorAll('a').forEach(a => {
  a.addEventListener('click', () => {
    mobileMenu.classList.remove('open');
    menuToggle.setAttribute('aria-label', 'Open menu');
  });
});

// ── Keyboard: Cmd/Ctrl+Enter to generate ───────────────────────
$('prompt').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    genBtn.click();
  }
});

// ── Load existing sessions on startup ──────────────────────────
async function loadServerGallery() {
  try {
    const r = await fetch('/api/sessions', updateFetchOptions());
    const d = await r.json();
    if (d.sessions) {
      d.sessions.forEach(s => {
        (s.entries || []).forEach(e => {
          if (e.image_url && !state.gallery.find(g => g.url === e.image_url)) {
            state.gallery.push({ url: e.image_url, name: 'image.png', cost: e.cost || 0, model: e.model || '' });
          }
        });
      });
      renderGallery();
    }
  } catch (e) { /* offline ok */ }
}
loadServerGallery();
applyUrlParams();
