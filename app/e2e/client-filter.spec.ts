import type { Page } from '@playwright/test'
import { expect, loginApi, hydrated, uniq, test } from './support/fixtures'
import { clientCard, clientName, createClientUi } from './support/clients'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

test.describe('client-filter-a11y', () => {
  /**
   * Сегменты фильтра клиентов - переключатели на одной странице. Скринридер
   * должен понять, какой фильтр выбран: выбранный сегмент нажат
   * (aria-pressed="true"), остальные нет, и ни один не объявлен
   * «текущей страницей» (aria-current), раз путь у всех один.
   */

  const segments = (page: Page) => page.locator('.seg [role="button"]')
  const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

  async function expectOnlyPressed(page: Page, name: string) {
    await expect(segments(page)).toHaveCount(3)
    await expect(page.locator('.seg [role="button"][aria-pressed="true"]')).toHaveCount(1)
    await expect(segment(page, name)).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.seg [role="button"][aria-pressed="false"]')).toHaveCount(2)
    await expect(page.locator('.seg [aria-current]')).toHaveCount(0)
  }

  test('на /clients?f=exp нажат только «Истекают», «текущей страницы» среди сегментов нет', async ({ page }) => {
    await loginApi(page)
    await page.goto('/clients?f=exp')
    await expectOnlyPressed(page, 'Истекают')
  })

  test.describe('разметка сервера', () => {
    test.use({ javaScriptEnabled: false })

    test('без JS: на /clients?f=exp нажат только «Истекают», «текущей страницы» нет', async ({ page }) => {
      await loginApi(page)
      await page.goto('/clients?f=exp')
      await expectOnlyPressed(page, 'Истекают')
    })
  })

  test('после клика «Заморожены» нажат только он, «текущей страницы» среди сегментов нет', async ({ page }) => {
    await loginApi(page)
    await page.goto('/clients?f=exp')
    await expectOnlyPressed(page, 'Истекают')
    await segment(page, 'Заморожены').click()
    await expect(page).toHaveURL(/[?&]f=frz\b/)
    await expectOnlyPressed(page, 'Заморожены')
  })
})

test.describe('client-filter-early-click', () => {
  /**
   * Страница клиентов приходит с сервера готовой на вид: фильтры и поиск видны
   * раньше, чем Nuxt гидратирует её и повесит обработчики. Человек, кликнувший
   * «Истекают» или начавший печатать в поиск в это окно, должен получить свой
   * фильтр после готовности страницы, а не тихо потерянное действие.
   * Медленную гидратацию задаём детерминированно: JS-чанки придержаны.
   */

  interface Made { id: number, name: string }

  const DAY = 24 * 60 * 60 * 1000
  const soon = () => new Date(Date.now() + 3 * DAY)
  /** Сколько держать JS: окно между отрисовкой сервером и гидратацией. */
  const HOLD_MS = 1500

  async function ownClients(page: Page, prefix: string, specs: { suffix: string, expiresAt?: Date, frozen?: boolean }[]) {
    const made: Made[] = []
    for (const s of specs) {
      const name = `${prefix}-${s.suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null, expiresAt: s.expiresAt?.toISOString() ?? null } })
      expect(r.status(), await r.text()).toBe(200)
      const c = { id: (await r.json()).id, name }
      if (s.frozen) {
        const f = await page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
        expect(f.status(), await f.text()).toBe(202)
        await waitAccess(page.request, (await f.json()).operation)
      }
      made.push(c)
    }
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const response = await route.fetch()
      const list: { name: string }[] = await response.json()
      await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    return made
  }

  /** Придержать скрипты Nuxt: разметка и стили уже на экране, гидратации ещё нет. */
  async function slowHydration(page: Page) {
    await page.route(/\/_nuxt\/.*\.js(\?.*)?$/, async (route) => {
      await new Promise(r => setTimeout(r, HOLD_MS))
      await route.continue().catch(() => {})
    })
  }

  /** Открыть страницу и вернуть управление, как только пришла разметка. */
  async function openEarly(page: Page) {
    await page.goto('/clients', { waitUntil: 'commit' })
  }

  const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })
  const searchBox = (page: Page) => page.getByLabel('Поиск клиентов')

  async function cleanup(page: Page, made: Made[]) {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
  }

  test('клик «Истекают» до гидратации: после готовности фильтр включён, в списке только истекающие', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, uniq('fec'), [
      { suffix: 'a', expiresAt: soon() },
      { suffix: 'b' },
    ])
    const [a, b] = made as [Made, Made]
    try {
      await slowHydration(page)
      await openEarly(page)
      await segment(page, 'Истекают').click()
      await hydrated(page)
      await expect(clientCard(page, a.name)).toBeVisible()
      await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
      await expect(segment(page, 'Все')).not.toHaveClass(/\bon\b/)
      await expect(clientCard(page, b.name)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })

  test('клик «Заморожены» до гидратации: после готовности фильтр включён, в списке только замороженные', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, uniq('fec'), [
      { suffix: 'a', frozen: true },
      { suffix: 'b' },
    ])
    const [a, b] = made as [Made, Made]
    try {
      await slowHydration(page)
      await openEarly(page)
      await segment(page, 'Заморожены').click()
      await hydrated(page)
      await expect(clientCard(page, a.name)).toBeVisible()
      await expect(segment(page, 'Заморожены')).toHaveClass(/\bon\b/)
      await expect(segment(page, 'Все')).not.toHaveClass(/\bon\b/)
      await expect(clientCard(page, b.name)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })

  test('ввод в поиск до гидратации: после готовности текст на месте и список отфильтрован', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, uniq('fec'), [
      { suffix: 'alpha' },
      { suffix: 'beta' },
    ])
    const [a, b] = made as [Made, Made]
    try {
      await slowHydration(page)
      await openEarly(page)
      await searchBox(page).fill(a.name)
      await hydrated(page)
      await expect(clientCard(page, a.name)).toBeVisible()
      await expect(searchBox(page)).toHaveValue(a.name)
      await expect(clientCard(page, b.name)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })

  test('клик «Истекают» на готовой странице: фильтр включён, в списке только истекающие', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, uniq('fec'), [
      { suffix: 'a', expiresAt: soon() },
      { suffix: 'b' },
    ])
    const [a, b] = made as [Made, Made]
    try {
      await page.goto('/clients')
      await expect(clientCard(page, b.name)).toBeVisible()
      await segment(page, 'Истекают').click()
      await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
      await expect(clientCard(page, a.name)).toBeVisible()
      await expect(clientCard(page, b.name)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })
})

test.describe('client-filter-expiring', () => {
  /**
   * Фильтр «Истекают»: в нём только активные клиенты со сроком в ближайшие дни.
   * После удаления выбран видимый в нём сосед, после создания новый виден строкой:
   * не попавший в фильтр новый сбрасывает его, попавший - нет.
   */

  interface Made { id: number, name: string }

  const DAY = 24 * 60 * 60 * 1000
  /** Срок в окне «Истекают». */
  const soon = () => new Date(Date.now() + 3 * DAY)

  async function ownClients(page: Page, prefix: string, specs: { suffix: string, expiresAt?: Date, frozen?: boolean }[]) {
    const made: Made[] = []
    for (const s of specs) {
      const name = `${prefix}-${s.suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null, expiresAt: s.expiresAt?.toISOString() ?? null } })
      expect(r.status(), await r.text()).toBe(200)
      const c = { id: (await r.json()).id, name }
      if (s.frozen) {
        const f = await page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
        expect(f.status(), await f.text()).toBe(202)
        await waitAccess(page.request, (await f.json()).operation)
      }
      made.push(c)
    }
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const response = await route.fetch()
      const list: { name: string }[] = await response.json()
      await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    return made
  }

  const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

  async function openExpiring(page: Page) {
    await page.goto('/clients')
    await segment(page, 'Истекают').click()
    await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
  }

  async function select(page: Page, c: Made) {
    await clientCard(page, c.name).click()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
  }

  async function deleteSelected(page: Page, target: Made) {
    const panel = page.getByTestId('client-detail')
    await panel.getByRole('button', { name: 'Удалить клиента' }).click()
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'DELETE' && new URL(r.url()).pathname === `/api/clients/${target.id}`),
      page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click(),
    ])
    expect(response.status()).toBe(202)
    const { operation } = await response.json() as { operation: AccessOperation }
    await waitAccess(page.request, operation)
    await expect(clientCard(page, target.name)).toHaveCount(0)
  }

  async function create(page: Page, name: string, expires?: Date): Promise<Made> {
    const ddmmyyyy = expires && [expires.getDate(), expires.getMonth() + 1, expires.getFullYear()]
      .map((n, i) => String(n).padStart(i < 2 ? 2 : 4, '0')).join('.')
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients'),
      createClientUi(page, name, { expires: ddmmyyyy }),
    ])
    return { id: (await response.json()).id, name }
  }

  async function expectShown(page: Page, c: Made) {
    await page.waitForLoadState('networkidle')
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
    await expect(clientCard(page, c.name)).toBeVisible()
    await expect(clientCard(page, c.name)).toHaveAttribute('aria-pressed', 'true')
  }

  async function cleanup(page: Page, made: Made[]) {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
  }

  test('удаление при «Истекают»: выбран видимый сосед, а не замороженный со сроком между ними', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, uniq('fe'), [
      { suffix: 'a', expiresAt: soon() },
      { suffix: 'b', expiresAt: soon(), frozen: true },
      { suffix: 'c', expiresAt: soon() },
    ])
    const [a, b, c] = made as [Made, Made, Made]
    try {
      await openExpiring(page)
      await expect(clientCard(page, c.name)).toBeVisible()
      await expect(clientCard(page, b.name)).toHaveCount(0)
      await select(page, a)
      await deleteSelected(page, a)
      await expectShown(page, c)
    }
    finally { await cleanup(page, made) }
  })

  test('удаление единственного истекающего: не выбран никто, клиент без срока не открыт', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, uniq('fe'), [
      { suffix: 'a', expiresAt: soon() },
      { suffix: 'b' },
    ])
    const [a, b] = made as [Made, Made]
    try {
      await openExpiring(page)
      await expect(clientCard(page, b.name)).toHaveCount(0)
      await select(page, a)
      await deleteSelected(page, a)
      await page.waitForLoadState('networkidle')
      const panel = page.getByTestId('client-detail')
      await expect(panel.getByText('Выбери клиента в списке')).toBeVisible()
      await expect(clientName(panel)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })

  test('создание клиента без срока при «Истекают»: фильтр сброшен, новый выбран и виден', async ({ page }) => {
    await loginApi(page)
    const prefix = uniq('fe')
    const made = await ownClients(page, prefix, [{ suffix: 'a', expiresAt: soon() }])
    try {
      await openExpiring(page)
      await expect(clientCard(page, made[0]!.name)).toBeVisible()
      made.push(await create(page, `${prefix}-b`))
      await expectShown(page, made[1]!)
      await expect(segment(page, 'Все')).toHaveClass(/\bon\b/)
    }
    finally { await cleanup(page, made) }
  })

  test('создание клиента со сроком в окне при «Истекают»: фильтр остался, новый выбран и виден', async ({ page }) => {
    await loginApi(page)
    const prefix = uniq('fe')
    const made = await ownClients(page, prefix, [{ suffix: 'a', expiresAt: soon() }, { suffix: 'c' }])
    try {
      await openExpiring(page)
      await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
      made.push(await create(page, `${prefix}-b`, soon()))
      await expectShown(page, made[2]!)
      await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
      await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })
})
