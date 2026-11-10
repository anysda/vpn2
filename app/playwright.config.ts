import { defineConfig, devices } from '@playwright/test'
import { join } from 'node:path'

// Адрес панели и пароль admin приходят только из окружения: локально их ставит
// e2e/run-local.sh, для стенда — тот, кто запускает.
const baseURL = process.env.E2E_BASE_URL || 'http://127.0.0.1:31710'
const reportDir = process.env.E2E_REPORT_DIR || '.'
// Эталонные снимки — ручная сверка (VPN2-749): в шарды e2e не входят, идут отдельно с E2E_SHOTS=1 (shots.yml).
const shotsIgnore = process.env.E2E_SHOTS ? [] : [/(^|\/)screenshots\.spec\.ts$/]

export default defineConfig({
  testDir: './e2e',
  // Один админ на панель: параллельность — отдельные панели в run-shards.py.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: { pathTemplate: '{testDir}/__screenshots__/{projectName}/{arg}{ext}', threshold: 0, maxDiffPixels: 0 },
  },
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
    // Без флага chromium на Linux хинтует шрифт и округляет ширины букв до целых
    // пикселей: переносы строк не как на телефоне. Через run-server доходит только с --unsafe.
    // Без частичного растра тайл всегда рисуется целиком: эталонные снимки не плавают между запусками.
    launchOptions: { args: ['--font-render-hinting=none', '--disable-partial-raster'] },
    locale: 'ru-RU',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    acceptDownloads: true,
  },
  projects: [
    {
      name: 'desktop',
      // qr-size.spec.ts нужна панель с худшим входом, qrencode и zbarimg: его гоняет только e2e/qr-size.sh.
      testIgnore: [/(^|\/)narrow\.spec\.ts$/, /(^|\/)mobile(-[\w-]+)?\.spec\.ts$/, ...(process.env.QR_MODE ? [] : [/(^|\/)qr-size\.spec\.ts$/]), ...shotsIgnore],
      use: { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 1000 } },
    },
    {
      // Узкий десктоп: до 720px включается мобильный каркас, его держит проект mobile.
      name: 'narrow',
      testMatch: /(^|\/)(narrow|screenshots)\.spec\.ts$/,
      testIgnore: shotsIgnore,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1024, height: 768 } },
    },
    {
      // Все телефонные сценарии: mobile.spec.ts и mobile-<раздел>.spec.ts.
      name: 'mobile',
      testMatch: /(^|\/)(mobile(-[\w-]+)?|screenshots)\.spec\.ts$/,
      testIgnore: shotsIgnore,
      use: { ...devices['Desktop Chrome'], viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 },
    },
  ],
})
