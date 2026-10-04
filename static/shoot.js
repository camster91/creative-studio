/* Photoshoot page: upload -> vibe -> Go -> live pack -> download. */
(() => {
  'use strict';

  const options = JSON.parse(document.getElementById('shootOptions').textContent);
  const $ = (id) => document.getElementById(id);
  const form = $('setup');
  const photo = $('photo');
  const drop = $('drop');
  const goBtn = $('goBtn');
  const goLabel = $('goLabel');
  const notice = $('notice');
  const PACK_KEY = 'photogen_pack';
  const MAX_BYTES = 16 * 1024 * 1024;
  const TYPES = ['image/png', 'image/jpeg', 'image/webp'];
  const ACTIVE = ['queued', 'running'];

  const state = { file: null, me: null, pack: null, timer: null, starting: false, block: null };

  const sessionToken = () => localStorage.getItem('photogen_session') || '';
  const ownKey = () => localStorage.getItem('cs_api_key') || '';
  const authHeaders = () => {
    const headers = {};
    if (sessionToken()) headers['X-Session-Token'] = sessionToken();
    if (ownKey()) headers['X-API-Key'] = ownKey();
    return headers;
  };
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
  const escapeHtml = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ── Account & credits ────────────────────────────────────────────────
  async function loadMe() {
    if (!sessionToken()) { state.me = null; renderAccount(); refresh(); return; }
    try {
      const response = await fetch('/api/me', { headers: authHeaders() });
      state.me = response.ok ? await response.json() : null;
    } catch (_) { /* offline: keep the last known balance */ }
    renderAccount();
    refresh();
  }

  function renderAccount() {
    const link = $('accountLink');
    if (state.me) {
      const credits = state.me.credits_remaining;
      link.href = '/billing';
      link.innerHTML = `<span class="credit-dot${credits < 6 ? ' low' : ''}" aria-hidden="true"></span>${escapeHtml(plural(credits, 'credit'))}`;
      link.setAttribute('aria-label', `${plural(credits, 'credit')} left. Manage plan`);
    } else {
      link.href = '/signup';
      link.textContent = 'Sign in';
      link.removeAttribute('aria-label');
    }
  }

  // ── Selection & cost ─────────────────────────────────────────────────
  const selectedShots = () => [...form.querySelectorAll('input[name="shots"]:checked')].map((box) => box.value);
  const selectedTier = () => (form.querySelector('input[name="tier"]:checked') || {}).value || options.default_tier;
  const selectedVibe = () => (form.querySelector('input[name="vibe"]:checked') || {}).value || options.default_vibe;
  const packCost = () => selectedShots().length * (options.tiers[selectedTier()] || 1);

  function refresh() {
    const shots = selectedShots();
    const cost = packCost();
    const minutes = Math.max(1, Math.ceil((Math.ceil(shots.length / 3) * 50) / 60));
    $('costCredits').textContent = ownKey() && !state.me ? 'Uses your Gemini key' : plural(cost, 'credit');
    $('costDetail').textContent = `${plural(shots.length, 'image')} · about ${plural(minutes, 'minute')}`;

    const vibe = options.vibes.find((item) => item.id === selectedVibe()) || options.vibes[0];
    $('vibeHint').textContent = vibe.id === options.default_vibe
      ? `${vibe.blurb} Not sure? This works for almost every product.`
      : vibe.blurb;

    const balance = $('balance');
    if (state.me) {
      balance.hidden = false;
      balance.textContent = `You have ${plural(state.me.credits_remaining, 'credit')}.`;
    } else {
      balance.hidden = true;
    }

    if (!state.pack) {
      // Before a shoot, the results panel previews exactly the shots that will be made.
      document.querySelectorAll('.tile').forEach((tile) => { tile.hidden = !shots.includes(tile.dataset.shot); });
    }
    updateGo();
  }

  function updateGo() {
    const running = state.starting || (state.pack && ACTIVE.includes(state.pack.status));
    const shots = selectedShots();
    const cost = packCost();
    let block = null;
    if (!sessionToken() && !ownKey()) {
      block = { kind: 'signin', html: 'New here? Create a free account to get <strong>5 free credits</strong>. No card needed.' };
    } else if (state.me && state.me.credits_remaining === 0 && !ownKey()) {
      block = { kind: 'credits', html: 'You\'re out of credits. <a href="/billing">See plans</a> to keep shooting.' };
    } else if (state.me && state.me.credits_remaining < cost && !ownKey()) {
      block = {
        kind: 'credits',
        html: `This pack needs ${plural(cost, 'credit')} and you have ${state.me.credits_remaining}. Untick a shot under <strong>Customise</strong>, or <a href="/billing">top up</a>.`,
      };
    } else if (!shots.length) {
      block = { kind: 'shots', html: 'Pick at least one shot under <strong>Customise</strong>.' };
    }
    state.block = block;

    goBtn.classList.toggle('busy', Boolean(running));
    if (running) {
      goLabel.textContent = state.starting ? 'Starting…' : 'Shooting your pack…';
      goBtn.setAttribute('aria-disabled', 'true');
    } else if (block && block.kind === 'signin') {
      goLabel.textContent = 'Sign up free to start';
      goBtn.removeAttribute('aria-disabled');
    } else {
      goLabel.textContent = 'Create my photoshoot';
      if (block) goBtn.setAttribute('aria-disabled', 'true');
      else goBtn.removeAttribute('aria-disabled');
    }
    if (block && !running) showNotice(block.html, block.kind === 'signin' ? 'info' : 'warn');
    else if (!running && notice.dataset.kind !== 'error') hideNotice();
  }

  function showNotice(html, kind = 'info') {
    notice.innerHTML = html;
    notice.dataset.kind = kind;
    notice.classList.toggle('error', kind === 'error');
    notice.hidden = false;
  }
  function hideNotice() { notice.hidden = true; notice.dataset.kind = ''; }

  // ── Photo ────────────────────────────────────────────────────────────
  function setFile(file) {
    if (!file) return;
    if (!TYPES.includes(file.type)) {
      showNotice('That file type won\'t work. Use a PNG, JPG or WebP photo.', 'error');
      return;
    }
    if (file.size > MAX_BYTES) {
      showNotice('That photo is over 16 MB. Try a smaller export.', 'error');
      return;
    }
    state.file = file;
    const preview = $('photoPreview');
    if (preview.dataset.url) URL.revokeObjectURL(preview.dataset.url);
    preview.dataset.url = URL.createObjectURL(file);
    preview.src = preview.dataset.url;
    $('photoName').textContent = file.name;
    $('dropEmpty').hidden = true;
    $('dropFilled').hidden = false;
    if (notice.dataset.kind === 'error') hideNotice();
    updateGo();
  }

  photo.addEventListener('change', () => setFile(photo.files[0]));
  ['dragenter', 'dragover'].forEach((type) => drop.addEventListener(type, (event) => {
    event.preventDefault();
    drop.classList.add('dragging');
  }));
  ['dragleave', 'drop'].forEach((type) => drop.addEventListener(type, () => drop.classList.remove('dragging')));
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    setFile(event.dataTransfer.files[0]);
  });
  form.addEventListener('change', (event) => { if (event.target.name !== 'image') refresh(); });

  // ── Go ───────────────────────────────────────────────────────────────
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.starting || (state.pack && ACTIVE.includes(state.pack.status))) return;
    if (state.block && state.block.kind === 'signin') { window.location.href = '/signup'; return; }
    if (state.block) return; // the notice above the button already explains why
    if (!state.file) {
      showNotice('Add a product photo first, in step 1 above.', 'error');
      photo.focus();
      return;
    }
    const body = new FormData();
    body.append('image', state.file, state.file.name);
    body.append('vibe', selectedVibe());
    body.append('tier', selectedTier());
    body.append('shots', selectedShots().join(','));
    body.append('note', $('note').value.trim());

    state.starting = true;
    hideNotice();
    updateGo();
    try {
      const response = await fetch('/api/shoot', { method: 'POST', headers: authHeaders(), body });
      const data = await response.json().catch(() => ({}));
      if (response.status === 202) {
        sessionStorage.setItem(PACK_KEY, data.pack_id);
        startPack(data);
        $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
        loadMe();
      } else {
        showError(response.status, data);
      }
    } catch (_) {
      showNotice('We couldn\'t reach Photogen. Check your connection and try again. You weren\'t charged.', 'error');
    } finally {
      state.starting = false;
      updateGo();
    }
  });

  function showError(status, data) {
    const message = escapeHtml(data.message || data.error || 'Something went wrong. You weren\'t charged.');
    if (status === 402) {
      showNotice(`${message} <a href="/billing">See plans</a>`, 'error');
      loadMe();
    } else if (status === 401) {
      showNotice(`${message} <a href="/signup">Sign up free</a>`, 'error');
    } else if (status === 429) {
      showNotice('You\'re going a little fast. Wait a minute and try again. You weren\'t charged.', 'error');
    } else {
      showNotice(message, 'error');
    }
  }

  // ── Pack progress ────────────────────────────────────────────────────
  function startPack(pack) {
    state.pack = pack;
    renderPack(pack);
    clearTimeout(state.timer);
    if (ACTIVE.includes(pack.status)) state.timer = setTimeout(poll, 2500);
  }

  async function poll() {
    const id = state.pack && state.pack.pack_id;
    if (!id) return;
    try {
      const response = await fetch(`/api/shoot/${encodeURIComponent(id)}`, { headers: authHeaders() });
      if (response.status === 404) { resetPack(); return; }
      if (response.ok) {
        state.pack = await response.json();
        renderPack(state.pack);
      }
    } catch (_) { /* keep polling through a blip */ }
    if (state.pack && ACTIVE.includes(state.pack.status)) {
      state.timer = setTimeout(poll, 2500);
    } else {
      loadMe();
      updateGo();
    }
  }

  function renderPack(pack) {
    const outputs = Object.fromEntries(pack.outputs.map((output) => [output.id, output]));
    const finished = pack.outputs.filter((output) => ['done', 'failed'].includes(output.status)).length;
    const failed = pack.outputs.filter((output) => output.status === 'failed').length;
    const active = ACTIVE.includes(pack.status);

    $('progress').hidden = false;
    $('progressFill').style.width = `${Math.round((finished / pack.total) * 100)}%`;
    let text;
    let title;
    if (active) {
      title = 'Shooting your pack';
      text = `${pack.completed} of ${pack.total} ready. You can leave this page; we'll keep going.`;
    } else if (pack.status === 'done') {
      title = 'Your pack is ready';
      text = `All ${plural(pack.total, 'image')} are ready to download.`;
    } else if (pack.status === 'partial') {
      title = 'Your pack is ready';
      text = `${pack.completed} of ${pack.total} ready. ${plural(failed, 'shot')} didn't come out, so ${plural(pack.credits_refunded, 'credit')} went back to your balance.`;
    } else {
      title = 'This shoot didn\'t work';
      text = pack.credits_refunded
        ? `None of the shots came out. Your ${plural(pack.credits_refunded, 'credit')} are back in your balance. Try a photo of the product on a plain background.`
        : 'None of the shots came out. Try a photo of the product on a plain background.';
    }
    $('resultsTitle').textContent = title;
    $('resultsSub').textContent = active ? 'Each image appears here as soon as it\'s done.' : 'Click an image to view it full size.';
    $('progressText').textContent = text;
    $('resultsActions').hidden = active || !pack.download_url;

    const vibe = (options.vibes.find((item) => item.id === pack.vibe) || {}).label || '';
    document.querySelectorAll('.tile').forEach((tile) => {
      const output = outputs[tile.dataset.shot];
      tile.hidden = !output;
      if (!output || tile.dataset.state === output.status) return;
      tile.dataset.state = output.status;
      const frame = tile.querySelector('.frame');
      tile.querySelector('.tile-actions')?.remove();
      if (output.status === 'done') {
        frame.innerHTML = `<a class="tile-open" href="${escapeHtml(output.url)}" target="_blank" rel="noopener" aria-label="Open ${escapeHtml(output.label)} full size"><img src="${escapeHtml(output.url)}" alt="${escapeHtml(`${output.label}, ${vibe}`)}" loading="lazy"></a>`;
        const actions = document.createElement('div');
        actions.className = 'tile-actions';
        actions.innerHTML = `<a class="tile-btn" href="${escapeHtml(output.url)}" download="photogen-${escapeHtml(output.id)}-${escapeHtml(output.aspect.replace(':', 'x'))}.png">Download<span class="visually-hidden"> ${escapeHtml(output.label)}</span></a>`;
        tile.append(actions);
      } else if (output.status === 'failed') {
        frame.innerHTML = `<div class="tile-error">${escapeHtml(output.error || 'This shot didn\'t come out.')}</div>`;
      } else {
        frame.innerHTML = `<span class="tile-state">${output.status === 'running' ? 'Shooting…' : 'Up next'}</span>`;
      }
    });
    updateGo();
  }

  function resetPack() {
    clearTimeout(state.timer);
    state.pack = null;
    sessionStorage.removeItem(PACK_KEY);
    $('progress').hidden = true;
    $('resultsActions').hidden = true;
    $('resultsTitle').textContent = 'Your pack';
    $('resultsSub').textContent = 'Here\'s what you\'ll get. Everything is sized for where it\'s going.';
    document.querySelectorAll('.tile').forEach((tile) => {
      tile.dataset.state = 'idle';
      tile.querySelector('.tile-actions')?.remove();
      const ratio = (options.shots.find((shot) => shot.id === tile.dataset.shot) || {}).aspect || '';
      tile.querySelector('.frame').innerHTML = `<span class="frame-ratio" aria-hidden="true">${escapeHtml(ratio)}</span>`;
    });
    refresh();
  }

  $('againBtn').addEventListener('click', () => {
    resetPack();
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    form.querySelector('input[name="vibe"]:checked')?.focus();
  });

  $('zipBtn').addEventListener('click', async () => {
    const pack = state.pack;
    if (!pack || !pack.download_url) return;
    const button = $('zipBtn');
    button.setAttribute('aria-busy', 'true');
    button.textContent = 'Preparing ZIP…';
    try {
      const response = await fetch(pack.download_url, { headers: authHeaders() });
      if (!response.ok) throw new Error('download failed');
      const blob = await response.blob();
      const match = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') || '');
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = match ? match[1] : 'photogen-photoshoot.zip';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    } catch (_) {
      showNotice('The ZIP didn\'t download. Try again, or save the images one by one.', 'error');
    } finally {
      button.removeAttribute('aria-busy');
      button.textContent = 'Download all (ZIP)';
    }
  });

  // ── Start ────────────────────────────────────────────────────────────
  async function resume() {
    const id = sessionStorage.getItem(PACK_KEY);
    if (!id || (!sessionToken() && !ownKey())) return;
    try {
      const response = await fetch(`/api/shoot/${encodeURIComponent(id)}`, { headers: authHeaders() });
      if (response.ok) startPack(await response.json());
      else sessionStorage.removeItem(PACK_KEY);
    } catch (_) { /* nothing to resume */ }
  }

  refresh();
  loadMe();
  resume();
})();
