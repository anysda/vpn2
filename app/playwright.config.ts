import { defineConfig, devices } from '@playwright/test'

// Адрес панели и пароль admin приходят только из окружения: локально их ставит
// e2e/run-local.sh, для стенда — тот, кто запускает.
const baseURL = process.env.E2E_BASE_URL || 'http://127.0.0.1:51899'

export default defineConfig({
  testDir: './e2e',
  // Один админ на всю панель: 2FA и пароль тесты меняют по очереди.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  outputDir: 'test-results',
  globalTeardown: './e2e/support/teardown.ts',
  use: {
    baseURL,
    locale: 'ru-RU',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    acceptDownloads: true,
  },
  projects: [
    {
      name: 'desktop',
      testIgnore: /phone\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 1000 } },
    },
    {
      name: 'phone',
      testMatch: /phone\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: false },
    },
  ],
})
