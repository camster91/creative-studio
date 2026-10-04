const path = require('path');
const { test, expect } = require('@playwright/test');
const axeSource = require('axe-core').source;

// Set SHOOT_SCREENSHOTS=1 to refresh docs/screenshots/shoot-*.png.
const SCREENSHOTS = process.env.SHOOT_SCREENSHOTS === '1';
const SHOT_DIR = path.join(__dirname, '..', '..', 'docs', 'screenshots');
const PRODUCT = path.join(__dirname, 'fixtures', 'product.png');

test.use({ colorScheme: 'light' });

const viewports = [
  { name: 'phone', width: 390, height: 844 },
  { name: 'tablet', width: 820, height: 1180 },
  { name: 'desktop', width: 1440, height: 960 },
];

async function expectAccessible(page) {
  await page.addScriptTag({ content: axeSource });
  const result = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
  }));
  expect(result.violations, JSON.stringify(result.violations, null, 2)).toEqual([]);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
}

async function snap(page, name, fullPage = true) {
  if (SCREENSHOTS) await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`), fullPage });
}

// Placeholder "renders" for the mocked flow: illustrative only, not model output.
const VIBE_BG = { clean: ['#f6f5f2', '#e4e1da'], sunlit: ['#f3e3c7', '#d9b27c'] };
function placeholderSvg(shot, aspect) {
  const [w, h] = aspect.split(':').map((n) => Number(n) * 120);
  const [top, floor] = shot === 'cutout' ? ['#ffffff', '#ffffff'] : VIBE_BG.sunlit;
  const canH = Math.min(h * 0.55, w * 0.9);
  const canW = canH * 0.42;
  const x = shot === 'hero' ? w * 0.62 : (w - canW) / 2;
  const y = h * 0.82 - canH;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${top}"/><stop offset="1" stop-color="${floor}"/></linearGradient>
    <radialGradient id="s"><stop offset="0" stop-color="#000" stop-opacity=".28"/><stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient></defs>
    <rect width="100%" height="100%" fill="url(#g)"/>
    ${shot === 'cutout' ? '' : `<rect y="${h * 0.8}" width="100%" height="${h * 0.2}" fill="#b98e5c" opacity=".55"/>`}
    <ellipse cx="${x + canW / 2}" cy="${y + canH}" rx="${canW * 0.7}" ry="${canW * 0.12}" fill="url(#s)"/>
    <rect x="${x}" y="${y}" width="${canW}" height="${canH}" rx="${canW * 0.14}" fill="#e85d34"/>
    <rect x="${x + canW * 0.12}" y="${y + canH * 0.3}" width="${canW * 0.76}" height="${canH * 0.3}" rx="${canW * 0.08}" fill="#fff4e8"/>
    <circle cx="${x + canW / 2}" cy="${y + canH * 0.42}" r="${canW * 0.18}" fill="#e85d34"/>
  </svg>`;
}

const SHOTS = [
  ['hero', 'Hero banner', 'Website & email header', '16:9'],
  ['lifestyle', 'Lifestyle', 'Social posts & product pages', '4:5'],
  ['square', 'Square ad', 'Instagram & Facebook feed', '1:1'],
  ['portrait', 'Portrait ad', 'Feed ads (4:5)', '4:5'],
  ['story', 'Story ad', 'Stories, Reels & TikTok', '9:16'],
  ['cutout', 'White background', 'Amazon, Shopify & marketplaces', '1:1'],
];

function packState(doneCount, status) {
  const outputs = SHOTS.map(([id, label, use, aspect], index) => ({
    id, label, use, aspect,
    status: index < doneCount ? 'done' : (index < doneCount + 3 && status === 'running' ? 'running' : (status === 'running' ? 'pending' : 'done')),
    url: index < doneCount || status !== 'running' ? `/mock-render/${id}.svg` : null,
    error: null,
  }));
  return {
    pack_id: 'pk_0123456789abcdef01234567', status, vibe: 'sunlit', tier: 'balanced', provider: 'higgsfield',
    credits_charged: 6, credits_refunded: 0,
    completed: outputs.filter((o) => o.status === 'done').length, total: 6, outputs,
    download_url: status === 'running' ? null : '/api/shoot/pk_0123456789abcdef01234567/download',
  };
}

async function signedIn(page, credits) {
  await page.addInitScript(() => localStorage.setItem('photogen_session', 'browser-test-session'));
  await page.route('**/api/me', (route) => route.fulfill({ json: { email: 'a@b.co', credits_remaining: credits } }));
  await page.route('**/mock-render/*.svg', (route) => {
    const shot = route.request().url().split('/').pop().replace('.svg', '');
    const aspect = SHOTS.find((s) => s[0] === shot)[3];
    route.fulfill({ contentType: 'image/svg+xml', body: placeholderSvg(shot, aspect) });
  });
}

for (const scheme of ['light', 'dark']) {
  test.describe(`photoshoot ${scheme}`, () => {
    test.use({ colorScheme: scheme });
    for (const viewport of viewports) {
      test(`${viewport.name} signed-out page is accessible without overflow`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto('/shoot');
        await expect(page.getByRole('heading', { name: 'Your product photoshoot, in one click.' })).toBeVisible();
        await expect(page.locator('#goLabel')).toHaveText('Sign up free to start');
        await expect(page.locator('input[name="vibe"][value="clean"]')).toBeChecked();
        await expectAccessible(page);
        if (scheme === 'light' && viewport.name !== 'tablet') await snap(page, `shoot-signed-out-${viewport.name}`);
      });
    }
  });
}

test('one-button flow: upload, vibe, go, progress, pack ready', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await signedIn(page, 42);
  let polls = 0;
  await page.route('**/api/shoot', (route) => route.fulfill({ status: 202, json: packState(0, 'running') }));
  await page.route('**/api/shoot/pk_*', (route) => {
    polls += 1;
    route.fulfill({ json: polls === 1 ? packState(2, 'running') : packState(6, 'done') });
  });

  await page.goto('/shoot');
  await expect(page.locator('#accountLink')).toContainText('42 credits');
  await expect(page.locator('#costCredits')).toHaveText('6 credits');
  await expect(page.locator('#resultsActions')).toBeHidden();
  await page.locator('#photo').setInputFiles(PRODUCT);
  await expect(page.locator('#dropEmpty')).toBeHidden();
  await page.getByRole('radio', { name: /Sunlit natural/ }).check();
  await expect(page.locator('#photoName')).toHaveText('product.png');
  await expectAccessible(page);
  await snap(page, 'shoot-ready-desktop', false);

  await page.getByRole('button', { name: 'Create my photoshoot' }).click();
  await expect(page.locator('#progressText')).toContainText('2 of 6 ready');
  await expect(page.locator('#resultsTitle')).toHaveText('Shooting your pack');
  await snap(page, 'shoot-progress-desktop', false);

  await expect(page.locator('#resultsTitle')).toHaveText('Your pack is ready', { timeout: 10_000 });
  await expect(page.getByRole('button', { name: 'Download all (ZIP)' })).toBeVisible();
  await expect(page.getByRole('link', { name: /Download Story ad/ })).toHaveAttribute('download', 'photogen-story-9x16.png');
  await expect(page.locator('.tile img')).toHaveCount(6);
  await expectAccessible(page);
  await snap(page, 'shoot-done-desktop');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#results').scrollIntoViewIfNeeded();
  await page.evaluate(() => document.getElementById('results').scrollIntoView({ block: 'start' }));
  await snap(page, 'shoot-done-phone', false);
});

test('out-of-credits and not-enough-credits states explain the fix', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signedIn(page, 0);
  await page.goto('/shoot');
  await expect(page.locator('#notice')).toContainText('You\'re out of credits');
  await expect(page.locator('#notice a')).toHaveAttribute('href', '/billing');
  await expect(page.locator('#goBtn')).toHaveAttribute('aria-disabled', 'true');
  await expectAccessible(page);
  await snap(page, 'shoot-out-of-credits-phone', false);
});

test('a short balance suggests unticking a shot, and the cost updates live', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await signedIn(page, 5);
  await page.goto('/shoot');
  await expect(page.locator('#notice')).toContainText('needs 6 credits and you have 5');
  await page.locator('#advanced summary').click();
  await page.getByRole('radio', { name: /^Ultra/ }).check();
  await expect(page.locator('#costCredits')).toHaveText('30 credits');
  await page.getByRole('radio', { name: /^High/ }).check();
  await page.getByRole('checkbox', { name: /Story ad/ }).uncheck();
  await expect(page.locator('#costCredits')).toHaveText('10 credits');
  await expect(page.locator('.tile[data-shot="story"]')).toBeHidden();
  await page.getByRole('radio', { name: /^Standard/ }).check();
  await expect(page.locator('#costCredits')).toHaveText('5 credits');
  await expect(page.locator('#goBtn')).not.toHaveAttribute('aria-disabled', 'true');
  await expectAccessible(page);
  await snap(page, 'shoot-customise-desktop', false);
});
