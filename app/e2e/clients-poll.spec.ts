import type { Page, Request, Route, Locator } from '@playwright/test'
import { expect, hydrated, loginApi, uniq, type Guard, test } from './support/fixtures'
import { clientCard, clientName } from './support/clients'

test.describe('clients-poll', () => {
  /**
   * Список клиентов опрашивается одним таймером раз в 3 с, сколько бы
   * компонентов страницы ни читали его: запросы не обрывают друг друга, медленный
   * ответ списка всё равно доходит до экрана, а до первого ответа панель не
   * выдаёт «Пока нет клиентов» и «0 клиентов».
   */

  const isList = (r: Request) => r.method() === 'GET' && new URL(r.url()).pathname === '/api/clients'

  async function ownClient(page: Page) {
    const name = uniq('poll')
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    return { id: (await r.json()).id as number, name }
  }

  /** Ответ списка ждёт `ms`; запрос, оборванный самой страницей, не ошибка теста. */
  async function slow(route: Route, ms: number) {
    await new Promise(done => setTimeout(done, ms))
    await route.continue().catch(() => undefined)
  }

  const header = (page: Page) => page.locator('.head p')

  test('открытая карточка: не больше 4 GET списка за 9 с и ни одного оборванного', async ({ page }) => {
    await loginApi(page)
    const { id, name } = await ownClient(page)
    const gets: number[] = []
    const aborted: string[] = []
    page.on('request', (r) => { if (isList(r)) gets.push(Date.now()) })
    page.on('requestfailed', (r) => {
      if (isList(r) && r.failure()?.errorText.includes('ERR_ABORTED')) aborted.push(`${Date.now()} ${r.url()}`)
    })
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(clientCard(page, name)).toBeVisible()
    const start = gets[0]!
    await page.waitForTimeout(9000 - (Date.now() - start))
    const inWindow = gets.filter(t => t - start < 9000)
    expect(inWindow.length, `GET /api/clients за 9 с: ${inWindow.map(t => t - start).join(', ')} мс`).toBeLessThanOrEqual(4)
    expect(aborted, 'оборванные GET /api/clients').toEqual([])
    await page.request.delete(`/api/clients/${id}`)
  })

  test('каждый ответ списка идёт 4 с: список всё равно появляется', async ({ page }) => {
    test.setTimeout(60_000)
    await loginApi(page)
    const { id, name } = await ownClient(page)
    await page.route(url => url.pathname === '/api/clients', route => isList(route.request()) ? slow(route, 4000) : route.continue())
    await page.goto(`/clients?id=${id}`)
    await expect(clientCard(page, name)).toBeVisible({ timeout: 15_000 })
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.request.delete(`/api/clients/${id}`)
  })

  test('пока список грузится, нет «Пока нет клиентов» и «0 клиентов»', async ({ page }) => {
    await loginApi(page)
    const { id, name } = await ownClient(page)
    let release!: () => void
    const held = new Promise<void>((done) => { release = done })
    // Придержан не только первый ответ: повторные GET до него тоже ждут, иначе
    // пустоту закрыл бы ответ на повтор, а не на первый запрос.
    await page.route(url => url.pathname === '/api/clients', async (route) => {
      if (isList(route.request())) await held
      await route.continue().catch(() => undefined)
    })
    await page.goto('/clients')
    await hydrated(page)
    // Окно дольше такта опроса: ни первый рендер, ни тики до ответа не рисуют пустоту.
    for (let i = 0; i < 8; i++) {
      await expect(page.getByText('Пока нет клиентов')).toHaveCount(0)
      await expect(header(page)).not.toHaveText(/(^|\D)0 клиентов/)
      await page.waitForTimeout(500)
    }
    release()
    await expect(clientCard(page, name)).toBeVisible()
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.request.delete(`/api/clients/${id}`)
  })

  test('положительный контроль: без задержки список и счётчик, пустой ответ - «Пока нет клиентов»', async ({ page }) => {
    await loginApi(page)
    const { id, name } = await ownClient(page)
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(clientCard(page, name)).toBeVisible()
    // Счётчик шапки - по строкам на экране: список в базе меняют удаления соседних тестов.
    await expect(async () => {
      const total = await page.getByTestId('client-row').count()
      expect(await header(page).textContent()).toMatch(new RegExp(`^${total} клиент`))
    }).toPass()
    await page.request.delete(`/api/clients/${id}`)

    await page.route(url => url.pathname === '/api/clients', route => isList(route.request())
      ? route.fulfill({ json: [] })
      : route.continue())
    await page.goto('/clients')
    await hydrated(page)
    await expect(page.getByText('Пока нет клиентов')).toBeVisible()
    await expect(header(page)).toHaveText(/^0 клиентов · 0 устройств$/)
  })
})

test.describe('clients-poll-errors', () => {
  /**
   * Сбои опроса списка клиентов: зависший GET не замораживает опрос, ошибка API
   * до первой загрузки видна сообщением, а после загрузки не стирает список -
   * ни в одном случае панель не рисует ложные «Пока нет клиентов» и «0 клиентов».
   */

  const isList = (r: Request) => r.method() === 'GET' && new URL(r.url()).pathname === '/api/clients'
  const LIST = /\/api\/clients$/

  async function ownClient(page: Page) {
    const name = uniq('pollerr')
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
   * Нет ложной пустоты прямо сейчас: ни «Пока нет клиентов», ни «0 клиентов» в
   * шапке. Снимок, а не ожидание: ожидание дождалось бы следующего тика опроса.
   */
  async function noFalseEmpty(page: Page) {
    expect(await page.getByText('Пока нет клиентов').count(), '«Пока нет клиентов» на экране').toBe(0)
    expect(await header(page).textContent(), 'шапка').not.toMatch(/(^|\D)0 клиентов/)
  }

  test('первый GET списка не отвечает: опрос не замерзает, список появляется за 20 с', async ({ page }) => {
    test.setTimeout(60_000)
    await loginApi(page)
    const { id, name } = await ownClient(page)
    let first = true
    await page.route(url => url.pathname === '/api/clients', async (route) => {
      if (isList(route.request()) && first) {
        first = false
        // Ответа нет вовсе: запрос держится, пока его не бросит сама страница.
        await new Promise(done => setTimeout(done, 40_000))
      }
      await route.continue().catch(() => undefined)
    })
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(clientCard(page, name)).toBeVisible({ timeout: 20_000 })
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.request.delete(`/api/clients/${id}`)
  })

  test('API отвечает 500 первые 8 с: сообщение об ошибке без ложной пустоты, затем список', async ({ page, guard }) => {
    test.setTimeout(60_000)
    guard.allow(500, LIST, 'GET')
    await loginApi(page)
    const { id, name } = await ownClient(page)
    const until = Date.now() + 8000
    await page.route(url => url.pathname === '/api/clients', route => isList(route.request()) && Date.now() < until
      ? route.fulfill({ status: 500, json: { message: 'сбой списка' } })
      : route.continue())
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(loadError(page)).toBeVisible()
    while (Date.now() < until - 500) {
      await noFalseEmpty(page)
      expect(await loadError(page).isVisible(), 'сообщение об ошибке загрузки').toBe(true)
      await page.waitForTimeout(400)
    }
    await expect(clientCard(page, name)).toBeVisible({ timeout: 10_000 })
    await expect(loadError(page)).toHaveCount(0)
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.request.delete(`/api/clients/${id}`)
  })

  test('после загрузки один тик опроса отвечает 500: список и счётчик остаются', async ({ page, guard }) => {
    test.setTimeout(60_000)
    guard.allow(500, LIST, 'GET')
    await loginApi(page)
    const { id, name } = await ownClient(page)
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(clientCard(page, name)).toBeVisible()
    const before = await header(page).textContent()
    // Падает один тик целиком: $fetch повторяет GET после 500 сразу, повтор
    // тоже получает 500. Следующий тик через 3 с уже проходит.
    let failFrom = 0
    await page.route(url => url.pathname === '/api/clients', (route) => {
      if (!isList(route.request()) || (failFrom && Date.now() - failFrom > 1000)) return route.continue()
      failFrom ||= Date.now()
      return route.fulfill({ status: 500, json: { message: 'сбой списка' } })
    })
    await page.waitForResponse(r => isList(r.request()) && r.status() === 500, { timeout: 10_000 })
    // Следующий тик через 3 с: всё это время на экране прежний список.
    for (let i = 0; i < 5; i++) {
      await noFalseEmpty(page)
      expect(await clientCard(page, name).isVisible(), 'строка клиента').toBe(true)
      expect(await header(page).textContent(), 'шапка').toBe(before)
      await page.waitForTimeout(400)
    }
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.request.delete(`/api/clients/${id}`)
  })

  test('положительный контроль: без сбоев список и счётчик, сообщения об ошибке нет', async ({ page }) => {
    await loginApi(page)
    const { id, name } = await ownClient(page)
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(clientCard(page, name)).toBeVisible()
    await expect(async () => {
      const total = await page.getByTestId('client-row').count()
      expect(await header(page).textContent()).toMatch(new RegExp(`^${total} клиент`))
    }).toPass()
    await expect(loadError(page)).toHaveCount(0)
    await page.request.delete(`/api/clients/${id}`)
  })

  const VIEWPORTS = [
    { label: 'стол', use: { viewport: { width: 1400, height: 1000 }, isMobile: false, hasTouch: false } },
    { label: 'телефон', use: { viewport: { width: 402, height: 874 } } },
  ]

  for (const { label, use } of VIEWPORTS) {
    test.describe(label, () => {
      test.use(use)

      // Контроль - клиент B создан до прошлого визита: он уже в запомненном списке.
      for (const createdAfterVisit of [true, false]) {
        test(`возврат на /clients?id=B, B создан ${createdAfterVisit ? 'после' : 'до'} прошлого визита: открыт B`, async ({ page }) => {
          test.setTimeout(60_000)
          await loginApi(page)
          const a = await ownClient(page)
          let b = createdAfterVisit ? undefined : await ownClient(page)
          const detail = page.getByTestId('client-detail')
          await page.goto(`/clients?id=${a.id}`)
          await hydrated(page)
          // На телефоне по ?id= открыта карточка клиента, строка списка скрыта.
          await expect(clientName(detail)).toHaveText(a.name)
          await spaGo(page, '/')
          await expect(page).toHaveURL(/\/$/)
          b ??= await ownClient(page)
          // Первый ответ после возврата - не сразу: запомненный список успевает отрисоваться.
          await page.route(url => url.pathname === '/api/clients', async (route) => {
            if (isList(route.request())) await new Promise(done => setTimeout(done, 1500))
            await route.continue().catch(() => undefined)
          })
          await spaGo(page, `/clients?id=${b.id}`)
          await expect(clientName(detail)).toHaveText(b.name, { timeout: 10_000 })
          // Ещё такт опроса: выбор не уезжает ни на A, ни в список.
          await page.waitForTimeout(3500)
          expect(page.url(), 'адрес').toContain(`id=${b.id}`)
          expect(await clientName(detail).allTextContents(), 'открытый клиент').toEqual([b.name])
          await page.unrouteAll({ behavior: 'ignoreErrors' })
          await page.request.delete(`/api/clients/${a.id}`)
          await page.request.delete(`/api/clients/${b.id}`)
        })
      }
    })
  }

  test('после загрузки API 12 с отвечает 503: прежний список виден и помечен как устаревший', async ({ page, guard }) => {
    test.setTimeout(60_000)
    guard.allow(503, LIST, 'GET')
    await loginApi(page)
    const { id, name } = await ownClient(page)
    await page.goto(`/clients?id=${id}`)
    await hydrated(page)
    await expect(clientCard(page, name)).toBeVisible()
    const before = await header(page).textContent()
    let until = 0
    await page.route(url => url.pathname === '/api/clients', (route) => {
      if (!isList(route.request()) || (until && Date.now() > until)) return route.continue()
      until ||= Date.now() + 12_000
      return route.fulfill({ status: 503, json: { message: 'сервер недоступен' } })
    })
    await page.waitForResponse(r => isList(r.request()) && r.status() === 503, { timeout: 10_000 })
    await expect(staleError(page)).toBeVisible({ timeout: 5000 })
    await expect(staleError(page)).toContainText('ошибка сервера (код 503)')
    await expect(page.locator('body')).not.toContainText('сервер недоступен')
    while (Date.now() < until - 500) {
      expect(await staleError(page).isVisible(), 'сообщение об устаревшем списке').toBe(true)
      expect(await clientCard(page, name).isVisible(), 'строка клиента').toBe(true)
      expect(await header(page).textContent(), 'шапка').toBe(before)
      await page.waitForTimeout(400)
    }
    // API ожил: сообщение уходит со следующим удачным тиком.
    await expect(staleError(page)).toHaveCount(0, { timeout: 10_000 })
    await expect(clientCard(page, name)).toBeVisible()
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.request.delete(`/api/clients/${id}`)
  })
})

test.describe('clients-loading-text', () => {
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
})
