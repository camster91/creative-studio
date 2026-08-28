const { defineConfig } = require('@playwright/test');

const localChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

module.exports = defineConfig({
  testDir: './tests/browser',
  snapshotPathTemplate: '{testDir}/__snapshots__/{arg}{ext}',
  timeout: 30_000,
  expect: { timeout: 5_000, toHaveScreenshot: { maxDiffPixelRatio: 0.03 } },
  fullyParallel: false,
  reporter: process.env.CI ? 'github' : 'line',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    browserName: 'chromium',
    ...(process.env.CI ? {} : { launchOptions: { executablePath: localChrome } }),
    colorScheme: 'dark',
    reducedMotion: 'reduce',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'RATE_LIMIT_PER_MINUTE=1000 CREATIVE_DATA_DIR=/tmp/creative-studio-browser-test CREATIVE_OUTPUT_DIR=/tmp/creative-studio-browser-output .venv/bin/python scripts/creative-studio-web.py --host 127.0.0.1 --port 5173',
    url: 'http://127.0.0.1:5173/app',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
