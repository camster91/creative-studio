'use strict';

const form = document.getElementById('campaignForm');
const goBtn = document.getElementById('goBtn');
const statusEl = document.getElementById('goStatus');
let activeCampaignId = null;

function requestOptions(options = {}) {
  const headers = new Headers(options.headers || {});
  const session = localStorage.getItem('photogen_session') || '';
  const key = document.getElementById('apiKey').value.trim() || localStorage.getItem('creative_studio_api_key') || '';
  if (session) headers.set('X-Session-Token', session);
  if (key) headers.set('X-API-Key', key);
  return {...options, headers};
}

function lines(value) { return value.split('\n').map(v => v.trim()).filter(Boolean); }
function values(name) { return [...document.querySelectorAll(`#${name} input:checked`)].map(el => el.value); }

function payload() {
  const data = new FormData(form);
  return {
    brand: {name: data.get('brandName'), voice: data.get('brandVoice'), visual_rules: lines(data.get('visualRules')), forbidden_content: lines(data.get('forbiddenContent'))},
    product: {name: data.get('productName'), sku: data.get('sku'), facts: lines(data.get('facts')), approved_claims: lines(data.get('claims')), required_disclosures: lines(data.get('disclosures')), pack_asset_waived: data.get('packWaived') === 'on', pack_asset_waiver_reason: data.get('packWaiverReason')},
    work_order: {name: data.get('campaignName'), objective: data.get('objective'), audience: data.get('audience'), offer: data.get('offer'), channels: values('channels'), creative_direction: data.get('direction'), aspect_ratio: data.get('aspect'), tier: data.get('tier'), variations: Number(data.get('variations'))},
  };
}

function showReadiness(readiness) {
  const pct = Math.round((readiness.passed_count / readiness.check_count) * 100);
  document.getElementById('readinessMeter').style.width = `${pct}%`;
  document.getElementById('readinessTitle').textContent = readiness.ready ? 'Ready to generate' : `${readiness.passed_count} of ${readiness.check_count} checks passed`;
  document.getElementById('readinessList').innerHTML = readiness.ready
    ? '<li class="passed">Product truth and work order are complete.</li><li class="passed">Generation will use approved claims only.</li>'
    : readiness.missing.map(item => `<li>${item.message}</li>`).join('');
  goBtn.disabled = !readiness.ready;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  statusEl.textContent = 'Checking…';
  const response = await fetch('/api/campaigns', requestOptions({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload())}));
  const body = await response.json();
  if (!response.ok) {
    statusEl.textContent = response.status === 401 ? 'Sign in first, then return to this page.' : (body.error || 'Could not save campaign.');
    return;
  }
  activeCampaignId = body.id;
  const pack = form.elements.packAsset.files[0];
  if (pack) {
    statusEl.textContent = 'Validating and attaching the exact pack asset…';
    const upload = new FormData();
    upload.append('pack', pack);
    const packResponse = await fetch(`/api/campaigns/${encodeURIComponent(activeCampaignId)}/pack`, requestOptions({method: 'POST', body: upload}));
    const packBody = await packResponse.json();
    if (!packResponse.ok) { statusEl.textContent = packBody.error || 'Could not attach the pack asset.'; showReadiness(body.readiness); return; }
    body.readiness = packBody.readiness;
  }
  showReadiness(body.readiness);
  statusEl.textContent = body.readiness.ready ? 'Saved. Review the inputs, then press Go.' : 'Saved as a draft. Complete the missing items.';
  loadCampaigns();
});

goBtn.addEventListener('click', async () => {
  if (!activeCampaignId) return;
  const key = document.getElementById('apiKey').value.trim();
  if (key) localStorage.setItem('creative_studio_api_key', key);
  goBtn.disabled = true;
  statusEl.textContent = 'Building the approved generation plan…';
  const planResponse = await fetch(`/api/campaigns/${encodeURIComponent(activeCampaignId)}/go`, requestOptions({method: 'POST'}));
  const plan = await planResponse.json();
  if (!planResponse.ok) { statusEl.textContent = plan.error || 'Campaign could not start.'; goBtn.disabled = false; return; }
  statusEl.textContent = plan.generation_request.execution_mode === 'deterministic-pack-composite' ? 'Generating environments and compositing the approved pack…' : 'Generating concept-only campaign variations…';
  let generationResponse;
  if (plan.generation_request.execution_mode === 'deterministic-pack-composite') {
    const composite = new FormData();
    ['prompt', 'tier', 'aspect_ratio', 'variations', 'campaign_id'].forEach(key => composite.append(key, plan.generation_request[key]));
    generationResponse = await fetch('/api/composite', requestOptions({method: 'POST', body: composite}));
  } else {
    generationResponse = await fetch('/api/generate', requestOptions({method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': `campaign-${activeCampaignId}`}, body: JSON.stringify(plan.generation_request)}));
  }
  const generation = await generationResponse.json();
  if (!generationResponse.ok) { statusEl.textContent = generation.message || generation.error || 'Generation failed.'; goBtn.disabled = false; return; }
  if (generation.job_id) {
    statusEl.textContent = 'Campaign is running. Open Prompt Studio to watch the job and review outputs.';
  } else {
    statusEl.textContent = `Created ${(generation.images || []).length} campaign image(s). Open Prompt Studio to review them.`;
  }
  goBtn.disabled = false;
});

async function loadCampaigns() {
  const list = document.getElementById('campaignList');
  const response = await fetch('/api/campaigns', requestOptions());
  if (!response.ok) { list.innerHTML = '<p>Sign in to save and run campaigns.</p>'; return; }
  document.getElementById('signinNotice').hidden = true;
  document.getElementById('accountLink').textContent = 'Account active';
  document.getElementById('accountLink').href = '/app';
  const body = await response.json();
  list.innerHTML = body.campaigns.length ? body.campaigns.map(c => `<button class="campaign-row" data-id="${c.id}"><strong>${c.name || 'Untitled'}</strong><span>${c.brand_name} · ${c.product_name}</span><small>${c.status}</small></button>`).join('') : '<p>No campaigns yet.</p>';
  list.querySelectorAll('.campaign-row').forEach(button => button.addEventListener('click', async () => {
    const response = await fetch(`/api/campaigns/${button.dataset.id}`, requestOptions());
    if (!response.ok) return;
    const campaign = await response.json();
    activeCampaignId = campaign.id;
    showReadiness(campaign.readiness);
    statusEl.textContent = `Selected ${campaign.name}.`;
  }));
}

document.getElementById('apiKey').value = localStorage.getItem('creative_studio_api_key') || '';
form.elements.packWaived.addEventListener('change', event => {
  document.getElementById('waiverReasonLabel').hidden = !event.target.checked;
  form.elements.packAsset.disabled = event.target.checked;
});
form.elements.packAsset.addEventListener('change', event => {
  if (event.target.files.length) {
    form.elements.packWaived.checked = false;
    form.elements.packWaived.dispatchEvent(new Event('change'));
  }
});
loadCampaigns();
