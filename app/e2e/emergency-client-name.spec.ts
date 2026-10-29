import { test, expect, type Browser, type BrowserContext, type Page } from '@playwright/test'
import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { extname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'

// VPN2-549: имя клиента в заголовке карточки аварийной панели на 390 обрезается
// многоточием, хотя место есть. Короткое и среднее имя (до 16 знаков) - целиком,
// длинное - многоточием в одну строку, без горизонтальной прокрутки.
// Имена подставляются в ответ /api/state: фикстура знает только свои.
// Статика собирается в свой каталог и отдаётся из него: emergency.spec.ts в
// соседнем шарде пересобирает общий static/ и затёр бы её посреди прогона.

const types: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.png': 'image/png' }

let directory: string
let assets: string
let fixture: ChildProcessWithoutNullStreams
let startup: { port: number, password: string }

test.beforeAll(async () => {
  test.setTimeout(120_000)
  directory = await mkdtemp(join(tmpdir(), 'emergency-name-'))
  assets = join(directory, 'static')
  execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts', '--outDir', assets, '--emptyOutDir'], { cwd: process.cwd(), stdio: 'pipe' })
  await writeFile(join(directory, 'qr.png'), '')
  fixture = spawn('python3', [resolve('../infra/tests/emergency_web_fixture.py'), directory], { stdio: ['pipe', 'pipe', 'pipe'] })
  fixture.stderr.resume()
  const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
  startup = JSON.parse((await lines.next()).value!) as { port: number, password: string }
})

test.afterAll(async () => {
  fixture.stdin.end()
  await new Promise<void>(done => fixture.once('exit', () => done()))
  await rm(directory, { recursive: true, force: true })
})

// Клиенты фикстуры: 1 - без Telegram, «Подключён»; 2 - с Telegram, «Подключался».
async function open(browser: Browser, width: number, names: Record<number, string>): Promise<[BrowserContext, Page]> {
  const origin = `https://127.0.0.1:${startup.port}`
  const context = await browser.newContext({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: { width, height: width < 720 ? 844 : 1000 }, colorScheme: 'dark', reducedMotion: 'reduce' })
  await context.route(`${origin}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/state') {
      const response = await route.fetch()
      // До входа ответ 401 без тела: отдаём как есть.
      if (!response.ok()) return route.fulfill({ response })
      const state = await response.json() as { clients: { id: number, name: string }[] }
      for (const client of state.clients) client.name = names[client.id] ?? client.name
      return route.fulfill({ response, json: state })
    }
    if (path.startsWith('/api/')) return route.continue()
    const file = path === '/' ? 'index.html' : path.slice(1)
    return route.fulfill({ body: await readFile(join(assets, file)), contentType: types[extname(file)] ?? 'application/octet-stream' })
  })
  const page = await context.newPage()
  await page.goto(origin)
  await page.getByLabel('Пароль', { exact: true }).fill(startup.password)
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await expect(page.getByText('Возраст снимка:', { exact: false })).toBeVisible()
  return [context, page]
}

async function heading(page: Page, name: string) {
  await page.getByTestId('client-row').filter({ hasText: name }).click()
  const h2 = page.getByTestId('client-detail').locator('.cdh h2')
  await expect(h2).toHaveText(name)
  await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => undefined))))
  return h2.evaluate((e) => {
    const s = getComputedStyle(e)
    const row = e.closest('.cdh')!
    const children = [...row.children].map(c => Math.round(c.getBoundingClientRect().width))
    return {
      text: e.textContent, scrollWidth: e.scrollWidth, clientWidth: e.clientWidth, height: e.getBoundingClientRect().height,
      lineHeight: parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.5, textOverflow: s.textOverflow, hyphens: s.hyphens,
      row: Math.round(row.getBoundingClientRect().width), children,
      tag: (row.querySelector('.tag') as HTMLElement | null)?.textContent ?? null,
      pageScroll: document.documentElement.scrollWidth, innerWidth,
    }
  })
}

async function shot(page: Page, testInfo: { outputPath: (...p: string[]) => string }, name: string) {
  const shots = process.env.EMERGENCY_SHOTS || testInfo.outputPath('shots')
  await mkdir(shots, { recursive: true })
  await page.screenshot({ path: join(shots, name), fullPage: true })
}

test('390: имя 16 знаков рядом со статусом «Подключался» в заголовке карточки целиком', async ({ browser }, testInfo) => {
  const name = 'b17-badclient-16'
  const [context, page] = await open(browser, 390, { 2: name })
  try {
    const m = await heading(page, name)
    await shot(page, testInfo, 'emname-390-16.png')
    expect(m.scrollWidth, `имя обрезано при свободном месте: ${JSON.stringify(m)}`).toBeLessThanOrEqual(m.clientWidth + 1)
  }
  finally {
    await context.unrouteAll({ behavior: 'ignoreErrors' })
    await context.close()
  }
})

test('390: короткое имя 8 знаков в заголовке карточки целиком', async ({ browser }, testInfo) => {
  const name = 'b17-shrt'
  const [context, page] = await open(browser, 390, { 2: name })
  try {
    const m = await heading(page, name)
    await shot(page, testInfo, 'emname-390-8.png')
    expect(m.scrollWidth, `имя обрезано: ${JSON.stringify(m)}`).toBeLessThanOrEqual(m.clientWidth + 1)
  }
  finally {
    await context.unrouteAll({ behavior: 'ignoreErrors' })
    await context.close()
  }
})

test('390: длинное имя без пробелов - многоточие в одну строку, без горизонтальной прокрутки', async ({ browser }, testInfo) => {
  const name = 'b17-very-long-client-name-without-any-spaces-at-all'
  const [context, page] = await open(browser, 390, { 2: name })
  try {
    const m = await heading(page, name)
    await shot(page, testInfo, 'emname-390-long.png')
    const detail = JSON.stringify(m)
    expect(m.textOverflow, detail).toBe('ellipsis')
    expect(m.scrollWidth, detail).toBeGreaterThan(m.clientWidth)
    expect(m.hyphens, detail).not.toBe('auto')
    expect(m.height, detail).toBeLessThan(m.lineHeight * 1.5)
    expect(m.pageScroll, detail).toBeLessThanOrEqual(m.innerWidth)
  }
  finally {
    await context.unrouteAll({ behavior: 'ignoreErrors' })
    await context.close()
  }
})

test('1440: имя 16 знаков в заголовке карточки целиком', async ({ browser }, testInfo) => {
  const name = 'b17-badclient-16'
  const [context, page] = await open(browser, 1440, { 2: name })
  try {
    const m = await heading(page, name)
    await shot(page, testInfo, 'emname-1440-16.png')
    expect(m.scrollWidth, `имя обрезано: ${JSON.stringify(m)}`).toBeLessThanOrEqual(m.clientWidth + 1)
  }
  finally {
    await context.unrouteAll({ behavior: 'ignoreErrors' })
    await context.close()
  }
})
