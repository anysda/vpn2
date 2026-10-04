import type { Page, Request } from '@playwright/test'
import { expect, hydrated, loginApi, test, toast } from './support/fixtures'

// Тестовые значения: домены e2e-*, адреса из TEST-NET (RFC 5737) — их снесёт
// teardown, а на стенде они никуда не ведут.
const stamp = () => `${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`

function routesForm(page: Page) {
  return page.locator('form').filter({ has: page.getByPlaceholder('netflix.com / *.openai.com / 8.8.8.8/32') })
}

function chip(page: Page, value: string) {
  return page.locator('div[draggable="true"]').filter({ has: page.getByText(value, { exact: true }) })
}

function cellOf(page: Page, value: string) {
  return page.locator('div.rounded-md.p-3').filter({ has: chip(page, value) })
}

/** Счётчик POST /api/routes — ровно один запрос на одно добавление. */
function countPosts(page: Page) {
  const posts: Request[] = []
  page.on('request', (r) => {
    if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/routes') posts.push(r)
  })
  return posts
}

async function openRoutes(page: Page) {
  await loginApi(page)
  await page.goto('/')
  await hydrated(page)
  // Выбор выхода появляется, когда пришёл список выходов sing-box.
  await expect(routesForm(page).getByRole('combobox')).toContainText(/RU|NL|FI|US/)
}

async function pickExit(page: Page, label: RegExp) {
  await routesForm(page).getByRole('combobox').click()
  await page.getByRole('option', { name: label }).click()
}

async function addRule(page: Page, value: string, exit: RegExp) {
  await pickExit(page, exit)
  const input = routesForm(page).getByPlaceholder('netflix.com / *.openai.com / 8.8.8.8/32')
  await input.fill(value)
  const resp = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/routes')
  await routesForm(page).getByRole('button', { name: 'Добавить' }).click()
  expect((await resp).status()).toBe(200)
  await expect(toast(page, 'Правило добавлено')).toBeVisible()
  await expect(input).toHaveValue('')
  await expect(chip(page, value)).toBeVisible()
}

test.describe('маршрутизация', () => {
  test('домен, IP, подсеть и *.домен на разные выходы, перенос и удаление', async ({ page }) => {
    const posts = countPosts(page)
    await openRoutes(page)
    const s = stamp()
    const cases = [
      { value: `e2e-${s}.example.com`, exit: /NL$/, header: 'NL', warp: false },
      { value: `198.51.100.${10 + Math.floor(Math.random() * 200)}`, exit: /FI$/, header: 'FI', warp: false },
      { value: '203.0.113.0/24', exit: /US WARP$/, header: 'US WARP', warp: true },
      { value: `*.e2e-${s}.example.org`, exit: /RU$/, header: 'RU', warp: false },
    ]
    for (const c of cases) {
      await addRule(page, c.value, c.exit)
      const cell = cellOf(page, c.value)
      await expect(cell).toContainText(c.header)
      if (!c.warp) await expect(cell).not.toContainText('WARP')
    }
    expect(posts, 'одно добавление = один POST').toHaveLength(cases.length)
    await expect(page.getByText(/уже существует|Ошибка/)).toHaveCount(0)

    const stored: { value: string, type: string, outbound: string }[] = await (await page.request.get('/api/routes')).json()
    const byValue = new Map(stored.map(r => [r.value, r]))
    expect(byValue.get(cases[0]!.value)).toMatchObject({ type: 'domain', outbound: 'hy2-nl-direct' })
    expect(byValue.get(cases[1]!.value)).toMatchObject({ type: 'ip_cidr', outbound: 'hy2-fi-direct' })
    expect(byValue.get(cases[2]!.value)).toMatchObject({ type: 'ip_cidr', outbound: 'hy2-us-warp' })
    expect(byValue.get(cases[3]!.value)).toMatchObject({ type: 'domain', outbound: 'direct-ru' })

    // Изменение: перенос правила на другой выход перетаскиванием (PATCH).
    const moved = cases[0]!.value
    const target = page.locator('div.rounded-md.p-3').filter({ hasText: /FI WARP/ })
    const patched = page.waitForResponse(r => r.request().method() === 'PATCH' && /\/api\/routes\/\d+$/.test(r.url()))
    await chip(page, moved).dragTo(target)
    expect((await patched).status()).toBe(200)
    await expect(toast(page, `«${moved}» → 🇫🇮 FI WARP`)).toBeVisible()
    await expect(cellOf(page, moved)).toContainText('FI WARP')
    const after: { value: string, outbound: string }[] = await (await page.request.get('/api/routes')).json()
    expect(after.find(r => r.value === moved)?.outbound).toBe('hy2-fi-warp')

    // Удаление: крестик на чипе виден при наведении.
    for (const c of cases) {
      const ch = chip(page, c.value)
      await ch.hover()
      await ch.getByRole('button').click()
      await expect(toast(page, `«${c.value}» удалено`)).toBeVisible()
      await expect(ch).toHaveCount(0)
    }
    const left: { value: string }[] = await (await page.request.get('/api/routes')).json()
    expect(left.filter(r => cases.some(c => c.value === r.value))).toEqual([])
  })

  // VPN2-55: «правило появилось в списке, и тут же ошибка». Форма уходит по
  // Enter и по кнопке, а флаг загрузки не мешает второй отправке, пока первая
  // в полёте: второй POST получает 409 «уже существует» (или 500, если оба
  // прошли проверку на дубль раньше вставки).
  for (const how of ['двойной Enter', 'двойной клик по «Добавить»'] as const) {
    test(`${how} добавляет правило один раз и без ошибки`, async ({ page }) => {
      const posts = countPosts(page)
      await openRoutes(page)
      for (let i = 0; i < 5; i++) {
        const value = `e2e-dbl-${stamp()}.example.net`
        const input = routesForm(page).getByPlaceholder('netflix.com / *.openai.com / 8.8.8.8/32')
        await input.fill(value)
        const before = posts.length
        if (how === 'двойной Enter') {
          // Два submit подряд в одном такте — как второй Enter или автоповтор клавиши.
          await routesForm(page).evaluate((f: HTMLFormElement) => { f.requestSubmit(); f.requestSubmit() })
        }
        else {
          await routesForm(page).getByRole('button', { name: 'Добавить' }).dblclick()
        }
        await expect(chip(page, value)).toBeVisible()
        await expect(input).toHaveValue('')
        await page.waitForLoadState('networkidle')
        expect(posts.length - before, `попытка ${i + 1}: POST на одно правило`).toBe(1)
        await expect(page.getByText(/уже существует/)).toHaveCount(0)
      }
    })
  }

  test('кривое значение отклоняется понятной ошибкой', async ({ page, guard }) => {
    guard.allow(400, /\/api\/routes$/, 'POST')
    await openRoutes(page)
    const input = routesForm(page).getByPlaceholder('netflix.com / *.openai.com / 8.8.8.8/32')
    await input.fill('not a domain')
    await routesForm(page).getByRole('button', { name: 'Добавить' }).click()
    await expect(toast(page, /invalid_value/)).toBeVisible()
    await expect(input).toHaveValue('not a domain')
  })

  // VPN2-67: русский текст ошибки шёл в statusMessage, а h3 и Caddy оставляют
  // от него в строке статуса латиницу или «Conflict». Тост обязан показать
  // текст сервера дословно.
  test('дубль правила показывает русскую ошибку сервера дословно', async ({ page, guard }) => {
    guard.allow(409, /\/api\/routes$/, 'POST')
    await openRoutes(page)
    const value = `e2e-dup-${stamp()}.example.net`
    await addRule(page, value, /NL$/)
    const input = routesForm(page).getByPlaceholder('netflix.com / *.openai.com / 8.8.8.8/32')
    await input.fill(value)
    const resp = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/routes')
    await routesForm(page).getByRole('button', { name: 'Добавить' }).click()
    expect((await resp).status()).toBe(409)
    const text = `Правило «${value}» уже существует`
    await expect(toast(page, text)).toBeVisible()
    await expect(toast(page, text).getByText(text, { exact: true })).toBeVisible()
  })
})
