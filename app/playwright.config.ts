import { defineConfig, devices } from '@playwright/test'
import { join } from 'node:path'

// Адрес панели и пароль admin приходят только из окружения: локально их ставит
// e2e/run-local.sh, для стенда — тот, кто запускает.
const baseURL = process.env.E2E_BASE_URL || 'http://127.0.0.1:31710'
const reportDir = process.env.E2E_REPORT_DIR || '.'

export default defineConfig({
  testDir: './e2e',
  // Один админ на панель: параллельность — отдельные панели в run-shards.py.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: join(reportDir, 'playwright-report') }]],
  outputDir: join(reportDir, 'test-results'),
  globalTeardown: './e2e/support/teardown.ts',
  use: {
    baseURL,
    // APIRequestContext, в отличие от браузера, сам Origin не выставляет.
    extraHTTPHeaders: { origin: new URL(baseURL).origin },
    connectOptions: process.env.E2E_BROWSER_WS
      ? { wsEndpoint: process.env.E2E_BROWSER_WS, exposeNetwork: '<loopback>' }
      : undefined,
    locale: 'ru-RU',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    acceptDownloads: true,
  },
  projects: [
    {
      name: 'desktop',
      testIgnore: /narrow\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 1000 } },
    },
    {
      // Мобильной вёрстки нет: панель держит окно от 1024px.
      name: 'narrow',
      testMatch: /narrow\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1024, height: 768 } },
    },
  ],
})
