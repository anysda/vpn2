import type { Locator, Page, Request } from '@playwright/test'
import type { Guard } from './support/fixtures'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { clientCard } from './support/clients'

/**
 * До первого ответа списка клиентов в шапке и в списке пусто без текста: ни
 * «Загрузка…», ни «Пока нет клиентов», ни «0 клиентов». Причину сбоя загрузки
 * панель пишет сама, по-русски: ошибка сервера с кодом, нет связи, сервер не
 * ответил вовремя. Текст ответа сервера на страницу не попадает.
 */

const isList = (r: Request) => r.method() === 'GET' && new URL(r.url()).pathname === '/api/clients'
const LIST = /\/api\/clients$/

async function ownClient(page: Page) {
  const name = uniq('quiet')
  const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(r.status(), await r.text()).toBe(200)
  return { id: (await r.json()).id as number, name }
}

const header = (page: Page) => page.locator('.head p')
const loadError = (page: Page) => page.getByRole('alert').filter({ hasText: 'Не удалось загрузить клиентов' })
const staleError = (page: Page) => page.getByRole('alert').filter({ hasText: 'Не удалось обновить список клиентов' })

/** Переход внутри приложения, без перезагрузки: страница списка монтируется заново. */
async function spaGo(page: Page, path: string) {
  await page.evaluate(p => (document.querySelector('#__nuxt') as Element & {
    __vue_app__: { config: { globalProperties: { $router: { push: (p: string) => Promise<unknown> } } } }
  }).__vue_app__.config.globalProperties.$router.push(p), path)
}

/**
 * Каждое изменение DOM с первого кадра: текст шапки и списка, где есть
 * «Загрузка», «Пока нет клиентов» или «0 клиентов». Снимки раз в 500 мс
 * пропустили бы короткую вспышку между ними.
 */
async function watchFalseText(page: Page) {
  await page.addInitScript(() => {
    const seen: string[] = []
    const bad = /Загрузка|Пока нет клиентов|(^|\D)0 клиентов/
    const check = () => {
      for (const sel of ['.head', '.clist']) {
        const t = document.querySelector(sel)?.textContent ?? ''
        if (bad.test(t)) seen.push(`${sel}: ${t.replace(/\s+/g, ' ').trim().slice(0, 120)}`)
      }
    }
    ;(window as unknown as { falseText: string[] }).falseText = seen
    new MutationObserver(check).observe(document, { subtree: true, childList: true, characterData: true })
  })
  return async () => [...new Set(await page.evaluate(() => (window as unknown as { falseText: string[] }).falseText))]
}

/** Шапка прямо сейчас без подзаголовка: ни счётчика, ни слова о загрузке. */
async function bareHeader(page: Page) {
  expect((await header(page).allTextContents()).join('').trim(), 'подзаголовок шапки до ответа').toBe('')
}

/** GET списка держится, пока не отпустят; повторы до ответа тоже ждут. */
async function holdList(page: Page) {
  let release!: () => void
  const held = new Promise<void>((done) => { release = done })
  await page.route(url => url.pathname === '/api/clients', async (route) => {
    if (isList(route.request())) await held
    await route.continue().catch(() => undefined)
  })
  return release
}

/**
 * В сообщении - причина словами панели, без текста сервера и без голого
 * «Ошибка»: ни одной латинской буквы в сообщениях и на странице нет `server`.
 */
async function reasonOnly(page: Page, alert: Locator, reason: string, server?: string) {
  await expect(alert).toContainText(reason, { timeout: 20_000 })
  for (const text of await page.getByRole('alert').allTextContents()) {
    expect(text, 'сообщение об ошибке').not.toMatch(/[A-Za-z]/)
    expect(text, 'сообщение об ошибке').not.toMatch(/Ошибка/)
  }
  if (server) expect(await page.locator('body').innerText(), 'страница').not.toContain(server)
}

/** Обрыв GET браузер пишет в консоль: это ожидаемый шум теста, не ошибка панели. */
function dropAbortNoise(guard: Guard) {
  for (let i = guard.problems.length - 1; i >= 0; i--) {
    if (/ERR_FAILED/.test(guard.problems[i]!) && /\/api\/clients\b/.test(guard.problems[i]!)) guard.problems.splice(i, 1)
  }
}

test('ответ списка 4 с: до него шапка и список без текста, после - список и счётчик', async ({ page }) => {
  test.setTimeout(60_000)
  await loginApi(page)
  const { id, name } = await ownClient(page)
  const falseText = await watchFalseText(page)
  let answered = false
  await page.route(url => url.pathname === '/api/clients', async (route) => {
    if (isList(route.request()) && !answered) {
      await new Promise(done => setTimeout(done, 4000))
      answered = true
    }
    await route.continue().catch(() => undefined)
  })
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  while (!answered) {
    await bareHeader(page)
    await page.waitForTimeout(300)
  }
  await expect(clientCard(page, name)).toBeVisible({ timeout: 10_000 })
  await expect(header(page)).toHaveText(/^\d+ клиент.* · \d+ устройств/)
  expect(await falseText(), '«Загрузка», «Пока нет клиентов», «0 клиентов» за окно до ответа').toEqual([])
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('уход и возврат до первого ответа: снова пусто без текста, после ответа список', async ({ page }) => {
  test.setTimeout(60_000)
  await loginApi(page)
  const { id, name } = await ownClient(page)
  const falseText = await watchFalseText(page)
  const release = await holdList(page)
  await page.goto('/clients')
  await hydrated(page)
  await bareHeader(page)
  await spaGo(page, '/')
  await expect(page).toHaveURL(/\/$/)
  await spaGo(page, `/clients?id=${id}`)
  await expect(page).toHaveURL(/\/clients/)
  // Окно дольше такта опроса: ни монтирование, ни тики до ответа не рисуют текст.
  for (let i = 0; i < 10; i++) {
    await bareHeader(page)
    await page.waitForTimeout(400)
  }
  release()
  await expect(clientCard(page, name)).toBeVisible({ timeout: 10_000 })
  await expect(header(page)).toHaveText(/^\d+ клиент/)
  expect(await falseText(), '«Загрузка», «Пока нет клиентов», «0 клиентов» за оба визита').toEqual([])
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('первая загрузка 500 с английским текстом: «ошибка сервера (код 500)»', async ({ page, guard }) => {
  test.setTimeout(60_000)
  guard.allow(500, LIST, 'GET')
  await loginApi(page)
  await page.route(url => url.pathname === '/api/clients', route => isList(route.request())
    ? route.fulfill({ status: 500, json: { message: 'Internal Server Error' } })
    : route.continue())
  await page.goto('/clients')
  await hydrated(page)
  await expect(loadError(page)).toBeVisible()
  await reasonOnly(page, loadError(page), 'ошибка сервера (код 500)', 'Internal Server Error')
})

test('после загрузки 503: над прежним списком «ошибка сервера (код 503)»', async ({ page, guard }) => {
  test.setTimeout(60_000)
  guard.allow(503, LIST, 'GET')
  await loginApi(page)
  const { id, name } = await ownClient(page)
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(clientCard(page, name)).toBeVisible()
  await page.route(url => url.pathname === '/api/clients', route => isList(route.request())
    ? route.fulfill({ status: 503, json: { message: 'Service Unavailable' } })
    : route.continue())
  await expect(staleError(page)).toBeVisible({ timeout: 10_000 })
  await reasonOnly(page, staleError(page), 'ошибка сервера (код 503)', 'Service Unavailable')
  expect(await clientCard(page, name).isVisible(), 'прежний список').toBe(true)
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('обрыв соединения на первой загрузке: «нет связи с сервером»', async ({ page, guard }) => {
  test.setTimeout(60_000)
  await loginApi(page)
  await page.route(url => url.pathname === '/api/clients', route => isList(route.request())
    ? route.abort('failed')
    : route.continue())
  await page.goto('/clients')
  await hydrated(page)
  await expect(loadError(page)).toBeVisible({ timeout: 10_000 })
  await reasonOnly(page, loadError(page), 'нет связи с сервером')
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  dropAbortNoise(guard)
})

test('первый ответ дольше таймаута опроса: «сервер не ответил вовремя»', async ({ page }) => {
  test.setTimeout(60_000)
  await loginApi(page)
  await page.route(url => url.pathname === '/api/clients', async (route) => {
    // Ответа нет вовсе: запрос держится, пока его не бросит сама страница.
    if (isList(route.request())) await new Promise(done => setTimeout(done, 40_000))
    await route.continue().catch(() => undefined)
  })
  await page.goto('/clients')
  await hydrated(page)
  await expect(loadError(page)).toBeVisible({ timeout: 20_000 })
  await reasonOnly(page, loadError(page), 'сервер не ответил вовремя')
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})
