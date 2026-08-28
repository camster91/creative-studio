const { test, expect } = require('@playwright/test');
const axeSource = require('axe-core').source;

const viewports = [
  { name: 'phone', width: 360, height: 740 },
  { name: 'tablet', width: 768, height: 900 },
  { name: 'desktop', width: 1440, height: 900 },
];

for (const viewport of viewports) {
  test(`${viewport.name} layout has no workflow overflow`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/app');
    await expect(page.locator('#genBtn')).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

test('400% equivalent zoom keeps controls reachable', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto('/app');
  await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
  await expect(page.locator('#prompt')).toBeVisible();
  await expect(page.locator('#genBtn')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
});

test('critical flow passes automated accessibility and keyboard checks', async ({ page }) => {
  await page.goto('/app');
  await page.addScriptTag({ content: axeSource });
  const result = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
  }));
  expect(result.violations, JSON.stringify(result.violations, null, 2)).toEqual([]);

  await page.locator('#dropzone').focus();
  await expect(page.locator('#dropzone')).toBeFocused();
  const chooser = page.waitForEvent('filechooser');
  await page.keyboard.press('Enter');
  await (await chooser).setFiles({
    name: 'synthetic.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  });
  await expect(page.locator('#previewImg')).toBeVisible();
  await page.locator('#prompt').fill('Synthetic product scene');
  await expect(page.locator('#prompt')).toHaveValue('Synthetic product scene');
  await expect(page.locator('.template-card').first()).toBeVisible();
  await expect(page.locator('#generationStatus')).toHaveAttribute('aria-live', 'polite');

  const undersized = await page.locator('button:visible, [role="button"]:visible, input:visible, textarea:visible').evaluateAll(nodes =>
    nodes.filter(node => {
      const target = node.matches('input[type="checkbox"], input[type="radio"]') && node.closest('label') || node;
      const box = target.getBoundingClientRect();
      return box.width < 24 || box.height < 24;
    }).map(node => ({ id: node.id, text: node.textContent.trim(), box: node.getBoundingClientRect().toJSON() }))
  );
  expect(undersized).toEqual([]);

  await page.locator('#prompt').focus();
  await page.evaluate(() => lightboxOpen([{ url: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==' }], 0));
  await expect(page.locator('#lightbox')).toHaveAttribute('role', 'dialog');
  await expect(page.locator('#lightboxClose')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#prompt')).toBeFocused();
});

test('reduced motion and deterministic desktop baseline', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/app');
  await expect(page.locator('.template-card').first()).toBeVisible();
  expect(await page.locator('#btnSpinner').evaluate(node => parseFloat(getComputedStyle(node).animationDuration))).toBeLessThan(0.001);
  await expect(page).toHaveScreenshot('studio-desktop.png', { fullPage: true, animations: 'disabled' });
});

test('load, interaction, asset, and large-session budgets stay bounded', async ({ page }) => {
  await page.goto('/app');
  const budgets = await page.evaluate(() => {
    const navigation = performance.getEntriesByType('navigation')[0];
    const resources = performance.getEntriesByType('resource');
    const start = performance.now();
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < 200; i += 1) {
      const card = document.createElement('button');
      card.textContent = `Synthetic generation ${i}`;
      fragment.appendChild(card);
    }
    const host = document.createElement('div');
    host.hidden = true;
    host.appendChild(fragment);
    document.body.appendChild(host);
    const renderMs = performance.now() - start;
    host.remove();
    return {
      domContentLoadedMs: navigation.domContentLoadedEventEnd,
      scriptBytes: resources.filter(r => r.name.endsWith('.js')).reduce((n, r) => n + r.transferSize, 0),
      styleBytes: resources.filter(r => r.name.endsWith('.css')).reduce((n, r) => n + r.transferSize, 0),
      domNodes: document.querySelectorAll('*').length,
      render200Ms: renderMs,
      eagerOutputImages: [...document.querySelectorAll('.output-cell img')].filter(img => img.loading !== 'lazy').length,
    };
  });
  expect(budgets.domContentLoadedMs).toBeLessThan(1500);
  expect(budgets.scriptBytes).toBeLessThan(180_000);
  expect(budgets.styleBytes).toBeLessThan(120_000);
  expect(budgets.domNodes).toBeLessThan(900);
  expect(budgets.render200Ms).toBeLessThan(100);
  expect(budgets.eagerOutputImages).toBe(0);
});
