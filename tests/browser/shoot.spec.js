const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const axeSource = require('axe-core').source;

const SCREENSHOTS = process.env.SHOOT_SCREENSHOTS === '1';
const DOC_SHOT_DIR = path.join(__dirname, '..', '..', 'docs', 'screenshots');
const SHOT_DIR = process.env.SHOOT_SCREENSHOT_DIR || DOC_SHOT_DIR;
const ASSETS = path.join(__dirname, '..', '..', 'static', 'photogen');
const PRODUCT = path.join(ASSETS, 'yuzu-can-studio.webp');

test.use({ colorScheme: 'light' });
const viewports = [
  { name: 'phone', width: 390, height: 844 },
  { name: 'tablet', width: 820, height: 1180 },
  { name: 'desktop', width: 1440, height: 900 },
];

async function expectAccessible(page) {
  await page.addScriptTag({ content: axeSource });
  const result = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
  }));
  expect(result.violations, JSON.stringify(result.violations, null, 2)).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

async function snap(page, name, fullPage = true) {
  if (!SCREENSHOTS) return;
  await page.evaluate(async () => {
    await document.fonts.ready;
    document.querySelectorAll('img[loading="lazy"]').forEach((img) => { img.loading = 'eager'; });
    await Promise.all([...document.images].map((img) => img.decode().catch(() => {})));
  });
  for (const directory of new Set([DOC_SHOT_DIR, SHOT_DIR])) {
    fs.mkdirSync(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, `${name}.png`), fullPage });
  }
}

const SHOTS = [
  ['hero', 'Hero banner', 'Website and email header', '16:9'],
  ['lifestyle', 'Lifestyle', 'Social posts and product pages', '4:5'],
  ['square', 'Square ad', 'Instagram and Facebook feed', '1:1'],
  ['portrait', 'Portrait ad', 'Feed ads', '4:5'],
  ['story', 'Story ad', 'Stories, Reels and TikTok', '9:16'],
  ['cutout', 'White background', 'Amazon, Shopify and marketplaces', '1:1'],
];

function packState(doneCount, status) {
  const outputs = SHOTS.map(([id, label, use, aspect], index) => ({
    id, label, use, aspect,
    status: index < doneCount ? 'done' : (index < doneCount + 3 && status === 'running' ? 'running' : 'pending'),
    url: index < doneCount ? `/mock-render/${id}.webp` : null, error: null,
  }));
  return {
    pack_id: 'pk_0123456789abcdef01234567', status, vibe: 'bold', tier: 'balanced', provider: 'higgsfield',
    created_at: Date.now() / 1000 - 34, credits_charged: 6, credits_refunded: 0,
    completed: doneCount, total: 6, outputs,
    download_url: status === 'running' ? null : '/api/shoot/pk_0123456789abcdef01234567/download',
  };
}

async function selectedProvider(page, provider) {
  // The selected provider is in the page's public options, never a credential.
  await page.route('**/shoot', async (route) => {
    const response = await route.fetch();
    const html = (await response.text()).replace(/(<script type="application\/json" id="shootOptions">)(.*?)(<\/script>)/s,
      (_, start, json, end) => start + JSON.stringify({ ...JSON.parse(json), provider, accepts_user_key: provider === 'gemini' }) + end);
    await route.fulfill({ response, body: html });
  });
}

async function signedIn(page, credits) {
  await page.addInitScript(() => localStorage.setItem('photogen_session', 'browser-test-session'));
  await page.route('**/api/me', (route) => route.fulfill({ json: { email: 'a@b.co', credits_remaining: credits } }));
  await page.route('**/mock-render/*.webp', (route) => {
    const id = route.request().url().split('/').pop().replace('.webp', '');
    if (id === 'cutout') {
      // Clip the sample can into an illustrative white-background packshot.
      const photo = fs.readFileSync(PRODUCT).toString('base64');
      return route.fulfill({ contentType: 'image/svg+xml', body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1400 1400">
        <defs><clipPath id="can"><path d="M540 250 C530 175 865 175 870 250 L870 1060 C870 1180 530 1180 530 1060 Z"/></clipPath></defs>
        <rect width="1400" height="1400" fill="white"/><ellipse cx="700" cy="1150" rx="180" ry="24" fill="#E4E6EA"/>
        <image href="data:image/webp;base64,${photo}" width="1400" height="1400" clip-path="url(#can)"/>
      </svg>` });
    }
    return route.fulfill({ contentType: 'image/webp', path: path.join(ASSETS, id === 'hero' ? 'channel-email-16x9.webp' : 'yuzu-can-studio.webp') });
  });
}

for (const scheme of ['light', 'dark']) {
  test.describe(`photoshoot ${scheme} preference, on-brand light page`, () => {
    test.use({ colorScheme: scheme });
    for (const viewport of viewports) {
      test(`${viewport.name} signed-out page is accessible without overflow`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto('/shoot');
        await expect(page.getByRole('heading', { name: 'Your product, shot for every channel. Press Go.' })).toBeVisible();
        await expect(page.locator('#goLabel')).toHaveText('Sign up free to start');
        await expect(page.locator('input[name="vibe"][value="clean"]')).toBeChecked();
        await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
        await expectAccessible(page);
        if (scheme === 'light' && viewport.name !== 'tablet') await snap(page, `shoot-signed-out-${viewport.name}`);
      });
    }
  });
}

for (const viewport of viewports.filter((v) => v.name !== 'tablet')) {
  test(`one-button flow on ${viewport.name}: upload, vibe, progress, pack and downloads`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await signedIn(page, 42);
    let current = packState(2, 'running');
    await page.route('**/api/shoot', (route) => route.fulfill({ status: 202, json: packState(0, 'running') }));
    await page.route('**/api/shoot/pk_*', (route) => route.fulfill({ json: current }));
    await page.route('**/api/shoot/pk_*/download', (route) => route.fulfill({ contentType: 'application/zip', body: 'mock zip', headers: { 'Content-Disposition': 'attachment; filename="photoshoot.zip"' } }));

    await page.goto('/shoot');
    await expect(page.locator('#accountLink')).toContainText('42 credits');
    await expect(page.locator('#costCredits')).toHaveText('6 credits');
    await expect(page.locator('#resultsActions')).toBeHidden();
    await expect(page.locator('#advanced')).not.toHaveAttribute('open', '');
    await snap(page, `shoot-empty-${viewport.name}`);
    await page.locator('#photo').setInputFiles(PRODUCT);
    await expect(page.locator('#dropEmpty')).toBeHidden();
    await page.getByRole('radio', { name: /Bold colour/ }).check();
    await expect(page.locator('#drop')).toHaveCSS('background-color', 'rgb(255, 138, 107)');
    await expect(page.locator('#photoName')).toHaveText('yuzu-can-studio.webp');
    const circle = await page.locator('#goBtn').boundingBox();
    expect(circle.width).toBe(circle.height);
    if (viewport.name === 'desktop') expect(circle.width).toBeGreaterThanOrEqual(120);
    await expectAccessible(page);
    await snap(page, `shoot-ready-${viewport.name}`);

    await page.getByRole('button', { name: 'Create my photoshoot' }).click();
    await expect(page.locator('#progressText')).toContainText('2 of 6 ready');
    await expect(page.locator('#resultsTitle')).toHaveText('Shooting your pack');
    await expect(page.locator('#elapsed')).toContainText('elapsed');
    await expectAccessible(page);
    await snap(page, `shoot-progress-${viewport.name}`);
    current = packState(6, 'done');

    await expect(page.locator('#resultsTitle')).toHaveText('Your pack is ready', { timeout: 10_000 });
    await expect(page.getByRole('link', { name: /Download Story ad/ })).toHaveAttribute('download', 'photogen-story-9x16.png');
    await expect(page.locator('.tile img')).toHaveCount(6);
    await expect(page.locator('#labelCheck')).toContainText('Check every label');
    await expectAccessible(page);
    await snap(page, `shoot-done-${viewport.name}`);
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download all (ZIP)' }).click();
    expect((await downloaded).suggestedFilename()).toBe('photoshoot.zip');
    await page.getByRole('button', { name: 'New shoot' }).click();
    await expect(page.locator('#resultsTitle')).toHaveText('Your pack');
    await expect(page.locator('#dropFilled')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('photogen_pack'))).toBeNull();
  });
}

test('out-of-credits state explains the fix on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signedIn(page, 0);
  await page.goto('/shoot');
  await expect(page.locator('#notice')).toContainText('You\'re out of credits');
  await expect(page.locator('#notice a')).toHaveAttribute('href', '/billing');
  await expect(page.locator('#goBtn')).toHaveAttribute('aria-disabled', 'true');
  await expectAccessible(page);
  await snap(page, 'shoot-out-of-credits-phone');
});

test('a short balance suggests fewer shots; customise shows the live cost', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signedIn(page, 5);
  await page.goto('/shoot');
  await page.locator('#photo').setInputFiles(PRODUCT);
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
  await snap(page, 'shoot-customise-desktop');
  for (const checkbox of await page.getByRole('checkbox').all()) await checkbox.uncheck();
  await expect(page.locator('#notice')).toContainText('Choose at least one shot');
  await expect(page.locator('#goBtn')).toHaveAttribute('aria-disabled', 'true');
});

test('a stored Gemini key never bypasses Higgsfield sign-in', async ({ page }) => {
  await selectedProvider(page, 'higgsfield');
  await page.addInitScript(() => localStorage.setItem('cs_api_key', 'test-own-key'));
  await page.goto('/shoot');
  await page.locator('#photo').setInputFiles(PRODUCT);
  await expect(page.getByRole('button', { name: 'Sign up free', exact: true })).toBeVisible();
  await expect(page.locator('#notice')).toContainText('5 trial credits');
  await expect(page.locator('#costCredits')).toHaveText('6 credits');
});

for (const credits of [0, 5]) {
  test(`a stored Gemini key never bypasses Higgsfield ${credits}-credit balance`, async ({ page }) => {
    await selectedProvider(page, 'higgsfield');
    await signedIn(page, credits);
    await page.addInitScript(() => localStorage.setItem('cs_api_key', 'test-own-key'));
    await page.goto('/shoot');
    await page.locator('#photo').setInputFiles(PRODUCT);
    await expect(page.locator('#notice')).toContainText(credits ? 'needs 6 credits and you have 5' : 'out of credits');
    await expect(page.locator('#goBtn')).toHaveAttribute('aria-disabled', 'true');
  });
}

test('Gemini own-key shoots stay available without credits', async ({ page }) => {
  await selectedProvider(page, 'gemini');
  await page.addInitScript(() => localStorage.setItem('cs_api_key', 'test-own-key'));
  await page.goto('/shoot');
  await page.locator('#photo').setInputFiles(PRODUCT);
  await expect(page.locator('#costCredits')).toHaveText('Uses your Gemini key');
  await expect(page.locator('#goBtn')).not.toHaveAttribute('aria-disabled', 'true');
});

test('returning to a running pack resumes progress and partial refunds', async ({ page }) => {
  await signedIn(page, 20);
  await page.addInitScript(() => localStorage.setItem('photogen_pack', 'pk_0123456789abcdef01234567'));
  let current = packState(2, 'running');
  await page.route('**/api/shoot/pk_*', (route) => route.fulfill({ json: current }));
  await page.goto('/shoot');
  await expect(page.locator('#progressText')).toContainText('2 of 6 ready');
  await page.reload();
  await expect(page.locator('#progressText')).toContainText('2 of 6 ready');
  current = packState(5, 'partial');
  current.outputs[5].status = 'failed';
  current.outputs[5].error = 'This shot did not come out.';
  current.credits_refunded = 1;
  await expect(page.locator('#progressText')).toContainText('1 credit went back to your balance');
  await expectAccessible(page);
});

test('bad file types and oversize photos explain how to fix the upload', async ({ page }) => {
  await signedIn(page, 20);
  await page.goto('/shoot');
  await page.locator('#photo').setInputFiles({ name: 'product.txt', mimeType: 'text/plain', buffer: Buffer.from('bad') });
  await expect(page.locator('#notice')).toContainText('Use a PNG, JPG or WebP');
  await page.locator('#photo').setInputFiles({ name: 'huge.png', mimeType: 'image/png', buffer: Buffer.alloc(16 * 1024 * 1024 + 1) });
  await expect(page.locator('#notice')).toContainText('over 16 MB');
  await page.locator('#photo').setInputFiles(PRODUCT);
  await expect(page.locator('#notice')).toBeHidden();
});
