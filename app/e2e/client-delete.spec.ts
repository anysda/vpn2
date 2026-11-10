import type { Page } from '@playwright/test'
import { expect, loginApi, uniq, test } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

test.describe('client-delete-filtered', () => {
  /**
   * После удаления выбранного клиента при активном поиске или фильтре сосед
   * берётся из видимого списка: клиент, скрытый поиском или фильтром, не
   * выбирается; не осталось видимых - не выбран никто.
   */

  interface Made { id: number, name: string }

  /** Свои клиенты с именами `<префикс>-<суффикс>`; список на странице сужен до них. */
  async function ownClients(page: Page, suffixes: string[]) {
    const prefix = uniq('df')
    const made: Made[] = []
    for (const suffix of suffixes) {
      const name = `${prefix}-${suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
      expect(r.status(), await r.text()).toBe(200)
      made.push({ id: (await r.json()).id, name })
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

  async function freeze(page: Page, c: Made) {
    const r = await page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
    expect(r.status(), await r.text()).toBe(202)
    await waitAccess(page.request, (await r.json()).operation)
  }

  async function select(page: Page, c: Made) {
    await clientCard(page, c.name).click()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
  }

  /** Удалить выбранного `target` из его карточки и дождаться конца операции. */
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

  /** Выбран `expected`: карточка его, строка видна и подсвечена; скрытые не открыты. */
  async function expectSelected(page: Page, expected: Made) {
    const panel = page.getByTestId('client-detail')
    await page.waitForLoadState('networkidle')
    await expect(clientName(panel)).toHaveText(expected.name)
    await expect(clientCard(page, expected.name)).toBeVisible()
    await expect(clientCard(page, expected.name)).toHaveAttribute('aria-pressed', 'true')
  }

  async function cleanup(page: Page, made: Made[]) {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
  }

  test('удаление при поиске: выбран видимый сосед, а не скрытый поиском между ними', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, ['a-keep', 'b-hide', 'c-keep'])
    const [a, b, c] = made as [Made, Made, Made]
    try {
      await page.goto('/clients')
      await page.getByPlaceholder('Поиск...').fill('keep')
      await expect(clientCard(page, b.name)).toHaveCount(0)
      await select(page, a)
      await deleteSelected(page, a)
      await expectSelected(page, c)
    }
    finally { await cleanup(page, made) }
  })

  test('удаление при фильтре «Заморожены»: выбран видимый сосед, а не скрытый фильтром', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, ['a', 'b', 'c'])
    const [a, b, c] = made as [Made, Made, Made]
    try {
      await freeze(page, a)
      await freeze(page, c)
      await page.goto('/clients')
      await page.getByRole('button', { name: /^Заморожены/ }).click()
      await expect(clientCard(page, b.name)).toHaveCount(0)
      await expect(clientCard(page, c.name)).toBeVisible()
      await select(page, a)
      await deleteSelected(page, a)
      await expectSelected(page, c)
    }
    finally { await cleanup(page, made) }
  })

  test('удаление последнего видимого при поиске: выбран видимый над ним, а не скрытый под ним', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, ['a-keep', 'b-keep', 'c-hide'])
    const [a, b, c] = made as [Made, Made, Made]
    try {
      await page.goto('/clients')
      await page.getByPlaceholder('Поиск...').fill('keep')
      await expect(clientCard(page, c.name)).toHaveCount(0)
      await select(page, b)
      await deleteSelected(page, b)
      await expectSelected(page, a)
    }
    finally { await cleanup(page, made) }
  })

  test('удаление единственного видимого при поиске: не выбран никто, скрытый не открыт', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, ['a-keep', 'b-hide'])
    const [a] = made as [Made, Made]
    try {
      await page.goto('/clients')
      await page.getByPlaceholder('Поиск...').fill('keep')
      await select(page, a)
      await deleteSelected(page, a)
      await page.waitForLoadState('networkidle')
      const panel = page.getByTestId('client-detail')
      await expect(panel.getByText('Выбери клиента в списке')).toBeVisible()
      await expect(clientName(panel)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })

  test('удаление без поиска и фильтра: выбран стоявший под ним', async ({ page }) => {
    await loginApi(page)
    const made = await ownClients(page, ['a', 'b', 'c'])
    const [a, b] = made as [Made, Made, Made]
    try {
      await page.goto('/clients')
      await select(page, a)
      await deleteSelected(page, a)
      await expectSelected(page, b)
    }
    finally { await cleanup(page, made) }
  })
})

test.describe('client-delete-neighbor', () => {
  /**
   * После удаления выбранного клиента панель выбирает того, кто стоял под ним
   * (удалён последний - того, кто над ним). Свежий список без удалённого может
   * прийти раньше, чем карточка дождётся подтверждения операции удаления, -
   * выбор от этого не меняется.
   */

  /** Три своих клиента a, b, c; список на странице сужен до них. */
  async function threeClients(page: Page) {
    const prefix = uniq('nb')
    const made: { id: number, name: string }[] = []
    for (const suffix of ['a', 'b', 'c']) {
      const name = `${prefix}-${suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
      expect(r.status(), await r.text()).toBe(200)
      made.push({ id: (await r.json()).id, name })
    }
    return { prefix, made }
  }

  /**
   * Удалить выбранного `target`: список после DELETE уже без него, а `confirmed`
   * операции страница может получить только после этого списка.
   */
  async function deleteBeforeConfirmed(page: Page, prefix: string, target: { id: number, name: string }) {
    const deleted: { operation?: Promise<AccessOperation> } = {}
    let listedWithout = 0
    let confirmedAt = 0
    await page.route(new RegExp(`/api/clients/${target.id}$`), async (route) => {
      if (route.request().method() !== 'DELETE') return route.fallback()
      const response = await route.fetch()
      deleted.operation = response.json().then((body: { operation: AccessOperation }) => body.operation)
      await route.fulfill({ response })
    })
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const response = await route.fetch()
      const list: { id: number, name: string }[] = await response.json()
      const own = list.filter(c => c.name.startsWith(prefix) && !(deleted.operation && c.id === target.id))
      // Новый опрос списка обрывает прежний запрос (useFetch отменяет его): отвечать уже некому.
      await route.fulfill({ response, json: own }).then(() => {
        if (deleted.operation) listedWithout ||= Date.now()
      }, (e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
      const response = await route.fetch()
      const state: AccessOperation = await response.json()
      // Список без удалённого ещё не дошёл до страницы: подтверждение придержать.
      const early = state.status === 'confirmed' && (!listedWithout || Date.now() < listedWithout + 1_000)
      if (state.status === 'confirmed' && !early) confirmedAt ||= Date.now()
      await route.fulfill({ response, json: early ? { ...state, status: 'pending' } : state })
    })
    const panel = page.getByTestId('client-detail')
    await panel.getByRole('button', { name: 'Удалить клиента' }).click()
    await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
    await expect.poll(() => listedWithout > 0, { timeout: 15_000 }).toBe(true)
    await expect(clientCard(page, target.name)).toHaveCount(0)
    // Сервер довёл удаление до конца: дальше страница видит только итоговое состояние.
    await waitAccess(page.request, await deleted.operation!)
    // Положительный контроль: список без удалённого дошёл до страницы раньше подтверждения.
    expect(!confirmedAt || confirmedAt > listedWithout, 'подтверждение пришло раньше списка').toBe(true)
  }

  for (const at of [1, 2]) {
    const what = at === 1 ? 'из середины: выбран стоявший под ним' : 'последнего: выбран стоявший над ним'
    test(`удаление клиента ${what}, хотя список обновился до подтверждения`, async ({ page }, testInfo) => {
      await loginApi(page)
      const { prefix, made } = await threeClients(page)
      const target = made[at]!
      const expected = made[at === 2 ? 1 : 2]!
      try {
        const missing: string[] = []
        page.on('response', (r) => {
          if (r.status() === 404) missing.push(`${r.request().method()} ${r.url()}`)
        })
        await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
          if (route.request().method() !== 'GET') return route.fallback()
          const response = await route.fetch()
          const list: { name: string }[] = await response.json()
          await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
            if (!e.message.includes('already handled')) throw e
          })
        })
        await page.goto('/clients')
        const panel = page.getByTestId('client-detail')
        await expect(clientName(panel)).toHaveText(made[0]!.name)
        await clientCard(page, target.name).click()
        await expect(clientName(panel)).toHaveText(target.name)

        await deleteBeforeConfirmed(page, prefix, target)
        await expect(clientName(panel)).toHaveText(expected.name)
        await expect(clientCard(page, expected.name)).toHaveAttribute('aria-pressed', 'true')
        await page.waitForLoadState('networkidle')
        await expect(clientName(panel)).toHaveText(expected.name)
        expect(missing, 'GET удалённого клиента с 404').toEqual([])
        await page.screenshot({ path: testInfo.outputPath('after-delete.png') })
      }
      finally {
        // Не ждать обработчиков: route.fetch() запроса, который страница уже оборвала, может не вернуться.
        await page.unrouteAll({ behavior: 'ignoreErrors' })
        for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
      }
    })
  }
})

test.describe('client-delete-pending-empty', () => {
  /**
   * Удаление выбранного клиента идёт (экзит не подтвердил), и выбрать вместо него
   * некого: среди видимых остались только те, чьё удаление идёт. Правая часть
   * пуста, и обновления списка не открывают удаляемого снова.
   */

  interface Made { id: number, name: string }

  /**
   * Свои клиенты с суффиксами `suffixes`; список на странице сужен до них. Любое
   * удаление подвешено: список отдаёт клиента замороженным, операция - pending.
   */
  async function withHeldDeletes(page: Page, suffixes: string[]) {
    const prefix = uniq('pe')
    const made: Made[] = []
    for (const suffix of suffixes) {
      const name = `${prefix}-${suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
      expect(r.status(), await r.text()).toBe(200)
      made.push({ id: (await r.json()).id, name })
    }
    const held = new Map<number, string>()
    const rows = new Map<number, { id: number, name: string, status: string }>()
    const details = new Map<number, Record<string, unknown>>()
    for (const m of made) {
      const saved = await page.request.get(`/api/clients/${m.id}`)
      expect(saved.status()).toBe(200)
      details.set(m.id, { ...await saved.json(), status: 'frozen' })
    }
    const state = { heldListed: 0 }
    await page.route(/\/api\/clients\/\d+$/, async (route) => {
      const id = Number(new URL(route.request().url()).pathname.split('/').pop())
      if (!made.some(m => m.id === id)) return route.fallback()
      // Пока удаление висит, клиент для страницы есть: карточка отдаёт его, а не 404.
      if (route.request().method() === 'GET' && held.has(id)) return route.fulfill({ status: 200, json: details.get(id) })
      if (route.request().method() !== 'DELETE') return route.fallback()
      const response = await route.fetch()
      held.set(id, (await response.json() as { operation: AccessOperation }).operation.id)
      await route.fulfill({ response })
    })
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const holding = held.size > 0
      const response = await route.fetch()
      const list: { id: number, name: string, status: string }[] = await response.json()
      const own = list.filter(x => x.name.startsWith(prefix))
      for (const x of own) if (!held.has(x.id)) rows.set(x.id, x)
      const json = [...own.filter(x => !held.has(x.id)), ...[...held.keys()].map(id => ({ ...rows.get(id)!, status: 'frozen' }))]
      await route.fulfill({ response, json }).then(() => {
        if (holding) state.heldListed++
      }, (e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
      const response = await route.fetch()
      const op: AccessOperation = await response.json()
      // Удаления для страницы не заканчиваются до конца теста.
      const hold = [...held.values()].includes(op.id)
      await route.fulfill({ response, json: hold ? { ...op, status: 'pending' } : op })
    })
    return { prefix, made, state }
  }

  async function deleteSelected(page: Page) {
    await page.getByTestId('client-detail').getByRole('button', { name: 'Удалить клиента' }).click()
    await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
  }

  /** Записывать каждое имя, которое показывает карточка справа, начиная с этого момента. */
  async function recordShown(page: Page) {
    await page.evaluate(() => {
      const w = window as unknown as { shownNames: string[] }
      w.shownNames = []
      const read = () => {
        const name = document.querySelector('[data-testid="client-detail"] [title="Имя клиента"]')?.textContent?.trim()
        if (name) w.shownNames.push(name)
      }
      new MutationObserver(read).observe(document.body, { subtree: true, childList: true, characterData: true })
    })
  }
  const shownNames = (page: Page) => page.evaluate(() => (window as unknown as { shownNames: string[] }).shownNames)

  /** После удаления `y` справа пусто, и следующие 3+ с обновлений списка `y` не открывают. */
  async function staysEmpty(page: Page, state: { heldListed: number }, y: Made) {
    const detail = page.getByTestId('client-detail')
    await expect(detail).toContainText('Выбери клиента в списке', { timeout: 10_000 })
    await recordShown(page)
    // Положительный контроль подмены: y в списке, обновления списка с ним приходят.
    await expect(clientCard(page, y.name)).toBeVisible()
    const listed = state.heldListed
    const since = Date.now()
    await expect.poll(() => state.heldListed, { timeout: 15_000 }).toBeGreaterThan(listed + 1)
    if (Date.now() - since < 3500) await page.waitForTimeout(3500 - (Date.now() - since))
    await expect(detail).toContainText('Выбери клиента в списке')
    await expect(clientName(detail)).toHaveCount(0)
    await expect(clientCard(page, y.name)).not.toHaveAttribute('aria-pressed', 'true')
    expect(await shownNames(page)).not.toContain(y.name)
  }

  test('поиск оставил одного y, удаление y висит: справа пусто, y не открывается снова', async ({ page }) => {
    await loginApi(page)
    const { prefix, made, state } = await withHeldDeletes(page, ['y'])
    const [y] = made as [Made]
    try {
      await page.goto('/clients')
      await page.getByPlaceholder('Поиск...').fill(prefix)
      await clientCard(page, y.name).click()
      await expect(clientName(page.getByTestId('client-detail'))).toHaveText(y.name)
      await deleteSelected(page)
      await staysEmpty(page, state, y)
    }
    finally {
      await page.unrouteAll({ behavior: 'ignoreErrors' })
      for (const x of made) await page.request.delete(`/api/clients/${x.id}`)
    }
  })

  test('удаление x висит, удаление выбранного y висит: справа пусто, ни x, ни y не открываются', async ({ page }) => {
    await loginApi(page)
    const { prefix, made, state } = await withHeldDeletes(page, ['b-x', 'a-y'])
    const [x, y] = made as [Made, Made]
    try {
      await page.goto('/clients')
      await page.getByPlaceholder('Поиск...').fill(prefix)
      await clientCard(page, x.name).click()
      await expect(clientName(page.getByTestId('client-detail'))).toHaveText(x.name)
      await deleteSelected(page)
      await expect(clientName(page.getByTestId('client-detail'))).toHaveText(y.name, { timeout: 10_000 })
      await deleteSelected(page)
      await staysEmpty(page, state, y)
      expect(await shownNames(page)).not.toContain(x.name)
    }
    finally {
      await page.unrouteAll({ behavior: 'ignoreErrors' })
      for (const x of made) await page.request.delete(`/api/clients/${x.id}`)
    }
  })
})

test.describe('client-delete-pending-neighbor', () => {
  /**
   * Удаление одного клиента ещё идёт (экзит не подтвердил), и он остаётся в списке
   * замороженным. Удаление его соседа не выбирает его взамен: соседом берётся тот,
   * чьё удаление не идёт, и удаляемый справа не открывается ни на миг.
   */

  interface Made { id: number, name: string }

  /**
   * Три своих клиента a, b, c; список на странице сужен до них. Удаление `c`
   * подвешено: список отдаёт его замороженным, операция для страницы - pending.
   */
  async function threeWithPending(page: Page) {
    const prefix = uniq('pn')
    const made: Made[] = []
    for (const suffix of ['a', 'b', 'c']) {
      const name = `${prefix}-${suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
      expect(r.status(), await r.text()).toBe(200)
      made.push({ id: (await r.json()).id, name })
    }
    const c = made[2]!
    const state = { cDeleted: false, operationId: '', frozenListed: 0 }
    let row: { id: number, name: string, status: string } | undefined
    const saved = await page.request.get(`/api/clients/${c.id}`)
    expect(saved.status()).toBe(200)
    const detail = { ...await saved.json(), status: 'frozen' }
    await page.route(new RegExp(`/api/clients/${c.id}$`), async (route) => {
      // Пока удаление висит, клиент для страницы есть: карточка отдаёт его, а не 404.
      if (route.request().method() === 'GET' && state.cDeleted) return route.fulfill({ status: 200, json: detail })
      if (route.request().method() !== 'DELETE') return route.fallback()
      const response = await route.fetch()
      state.operationId = (await response.json() as { operation: AccessOperation }).operation.id
      state.cDeleted = true
      await route.fulfill({ response })
    })
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const pending = state.cDeleted
      const response = await route.fetch()
      const list: { id: number, name: string, status: string }[] = await response.json()
      const own = list.filter(x => x.name.startsWith(prefix))
      const seen = own.find(x => x.id === c.id)
      if (!pending && seen) row = seen
      if (pending) own.splice(0, own.length, ...own.filter(x => x.id !== c.id), { ...(seen ?? row!), status: 'frozen' })
      await route.fulfill({ response, json: own }).then(() => {
        if (pending) state.frozenListed++
      }, (e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
      const response = await route.fetch()
      const op: AccessOperation = await response.json()
      // Удаление c для страницы не заканчивается до конца теста.
      const hold = state.operationId !== '' && op.id === state.operationId
      await route.fulfill({ response, json: hold ? { ...op, status: 'pending' } : op })
    })
    return { prefix, made, state }
  }

  async function select(page: Page, c: Made) {
    await clientCard(page, c.name).click()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
  }

  async function deleteSelected(page: Page) {
    await page.getByTestId('client-detail').getByRole('button', { name: 'Удалить клиента' }).click()
    await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
  }

  /** Записывать каждое имя, которое показывает карточка справа, начиная с этого момента. */
  async function recordShown(page: Page) {
    await page.evaluate(() => {
      const w = window as unknown as { shownNames: string[] }
      w.shownNames = []
      const read = () => {
        const name = document.querySelector('[data-testid="client-detail"] [title="Имя клиента"]')?.textContent?.trim()
        if (name) w.shownNames.push(name)
      }
      new MutationObserver(read).observe(document.body, { subtree: true, childList: true, characterData: true })
    })
  }
  const shownNames = (page: Page) => page.evaluate(() => (window as unknown as { shownNames: string[] }).shownNames)

  for (const view of ['all', 'search'] as const) {
    const what = view === 'all' ? '«Все»' : 'поиск'
    test(`${what}: удаление c висит, удаление b выбирает a, а не удаляемого c`, async ({ page }) => {
      await loginApi(page)
      const { prefix, made, state } = await threeWithPending(page)
      const [a, b, c] = made as [Made, Made, Made]
      try {
        await page.goto('/clients')
        if (view === 'search') await page.getByPlaceholder('Поиск...').fill(prefix)
        for (const x of made) await expect(clientCard(page, x.name)).toBeVisible()
        await select(page, c)
        await deleteSelected(page)
        // Положительный контроль подмены: c в списке замороженным, выбор ушёл на b.
        await expect.poll(() => state.frozenListed, { timeout: 15_000 }).toBeGreaterThan(0)
        await expect(clientCard(page, c.name)).toBeVisible()
        await expect(clientName(page.getByTestId('client-detail'))).toHaveText(b.name)

        await recordShown(page)
        await deleteSelected(page)
        await expect(clientName(page.getByTestId('client-detail'))).toHaveText(a.name)
        await expect(clientCard(page, a.name)).toHaveAttribute('aria-pressed', 'true')
        // Список с c ещё приходит, а выбор держится на a и c не открывался ни разу.
        const listed = state.frozenListed
        await expect.poll(() => state.frozenListed, { timeout: 15_000 }).toBeGreaterThan(listed + 1)
        await expect(clientName(page.getByTestId('client-detail'))).toHaveText(a.name)
        expect(await shownNames(page)).not.toContain(c.name)
      }
      finally {
        await page.unrouteAll({ behavior: 'ignoreErrors' })
        for (const x of made) await page.request.delete(`/api/clients/${x.id}`)
      }
    })
  }
})

test.describe('client-delete-transient-status', () => {
  /**
   * Пока идёт удаление, список отдаёт удаляемого клиента промежуточным статусом
   * (frozen), и он выпадает из фильтра «Истекают». После удаления всё равно выбран
   * сосед из того, что видно слева, а удалённый на это время не остаётся открытым.
   */

  interface Made { id: number, name: string }

  const DAY = 24 * 60 * 60 * 1000
  /** Срок в окне «Истекают». */
  const soon = () => new Date(Date.now() + 3 * DAY).toISOString()

  /** Ход удаления, как его видит страница: список и подтверждение операции. */
  interface Deletion {
    /** `none` - DELETE ещё не ушёл, `frozen` - удаляемый в списке замороженным, `gone` - его нет. */
    phase: 'none' | 'frozen' | 'gone'
    /** Сколько ответов списка с замороженным удаляемым дошло до страницы. */
    frozenListed: number
    /** Строка удаляемого в списке до DELETE. */
    row?: { id: number, name: string, status: string }
    operation?: Promise<AccessOperation>
  }

  /**
   * Три своих клиента a, b, c со сроком в окне; список на странице сужен до них.
   * Удаление `target` подменено: после DELETE список отдаёт его замороженным, пока
   * тест не переведёт `phase` в `gone`, а подтверждение операции до того придержано.
   */
  async function threeExpiring(page: Page, target: 'a' | 'b' | 'c') {
    const prefix = uniq('ts')
    const made: Made[] = []
    for (const suffix of ['a', 'b', 'c']) {
      const name = `${prefix}-${suffix}`
      const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null, expiresAt: soon() } })
      expect(r.status(), await r.text()).toBe(200)
      made.push({ id: (await r.json()).id, name })
    }
    const victim = made[['a', 'b', 'c'].indexOf(target)]!
    const del: Deletion = { phase: 'none', frozenListed: 0 }
    const saved = await page.request.get(`/api/clients/${victim.id}`)
    expect(saved.status()).toBe(200)
    const detail = { ...await saved.json(), status: 'frozen' }
    await page.route(new RegExp(`/api/clients/${victim.id}$`), async (route) => {
      // В окне frozen клиент ещё есть в базе: карточка его отдаёт, а не 404.
      if (route.request().method() === 'GET' && del.phase === 'frozen') {
        const response = await route.fetch()
        if (response.status() !== 404) return route.fulfill({ response, json: { ...await response.json(), status: 'frozen' } })
        return route.fulfill({ status: 200, json: detail })
      }
      if (route.request().method() !== 'DELETE') return route.fallback()
      const response = await route.fetch()
      del.operation = response.json().then((body: { operation: AccessOperation }) => body.operation)
      del.phase = 'frozen'
      await route.fulfill({ response })
    })
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const phase = del.phase
      const response = await route.fetch()
      const list: { id: number, name: string, status: string }[] = await response.json()
      const own = list.filter(c => c.name.startsWith(prefix) && !(phase === 'gone' && c.id === victim.id))
      // До DELETE запомнить строку удаляемого; в окне frozen она в ответе, даже если
      // сервер уже успел его убрать (настоящее окно ~200 мс тест растягивает).
      const row = own.find(c => c.id === victim.id)
      if (phase === 'none' && row) del.row = row
      if (phase === 'frozen') {
        const rest = own.filter(c => c.id !== victim.id)
        own.splice(0, own.length, ...rest, { ...(row ?? del.row!), status: 'frozen' })
      }
      // Новый опрос списка обрывает прежний запрос (useFetch отменяет его): отвечать уже некому.
      await route.fulfill({ response, json: own }).then(() => {
        if (phase === 'frozen' && own.some(c => c.id === victim.id)) del.frozenListed++
      }, (e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
      const response = await route.fetch()
      const state: AccessOperation = await response.json()
      // Пока список отдаёт удаляемого замороженным, удаление для страницы не закончено.
      const early = state.status === 'confirmed' && del.phase !== 'gone'
      await route.fulfill({ response, json: early ? { ...state, status: 'pending' } : state })
    })
    return { prefix, made, victim, del }
  }

  const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

  type View = 'exp' | 'all' | 'search'

  async function open(page: Page, view: View, prefix: string, made: Made[]) {
    await page.goto('/clients')
    if (view === 'exp') {
      await segment(page, 'Истекают').click()
      await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
    }
    if (view === 'search') await page.getByPlaceholder('Поиск...').fill(prefix)
    for (const c of made) await expect(clientCard(page, c.name)).toBeVisible()
  }

  async function select(page: Page, c: Made) {
    await clientCard(page, c.name).click()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
  }

  /** Удалить выбранного и дождаться, пока страница получит список с ним замороженным. */
  async function deleteToFrozen(page: Page, del: Deletion) {
    await page.getByTestId('client-detail').getByRole('button', { name: 'Удалить клиента' }).click()
    await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
    // Положительный контроль подмены: промежуточный статус дошёл до страницы.
    await expect.poll(() => del.frozenListed, { timeout: 15_000 }).toBeGreaterThan(0)
  }

  /** Удаление закончено: удалённого нет ни в списке, ни в операции. */
  async function finish(page: Page, del: Deletion, victim: Made) {
    del.phase = 'gone'
    await waitAccess(page.request, await del.operation!)
    await expect(clientCard(page, victim.name)).toHaveCount(0)
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

  test('«Истекают», удаление среднего, в списке он сперва замороженный: выбран сосед снизу', async ({ page }) => {
    await loginApi(page)
    const { prefix, made, victim, del } = await threeExpiring(page, 'b')
    const [, b, c] = made as [Made, Made, Made]
    try {
      await open(page, 'exp', prefix, made)
      await select(page, b)
      await deleteToFrozen(page, del)
      await finish(page, del, victim)
      await expectShown(page, c)
    }
    finally { await cleanup(page, made) }
  })

  test('«Истекают», удаление среднего: пока он в списке замороженный, карточка справа его не показывает', async ({ page }) => {
    await loginApi(page)
    const { prefix, made, del } = await threeExpiring(page, 'b')
    const [, b] = made as [Made, Made, Made]
    try {
      await open(page, 'exp', prefix, made)
      await select(page, b)
      await deleteToFrozen(page, del)
      await expect(clientCard(page, b.name)).toHaveCount(0)
      // Не «когда-нибудь перестанет показывать», а ни разу за всё окно frozen:
      // проверка с повтором прошла бы, едва выбор уйдёт на кого угодно.
      await page.waitForTimeout(300)
      for (let i = 0; i < 10; i++) {
        expect(await clientName(page.getByTestId('client-detail')).allTextContents(), `${i * 200 + 300} мс после списка с замороженным`).not.toContain(b.name)
        await page.waitForTimeout(200)
      }
      expect(del.phase).toBe('frozen')
    }
    finally { await cleanup(page, made) }
  })

  test('«Истекают», удаление нижнего, в списке он сперва замороженный: выбран сосед сверху', async ({ page }) => {
    await loginApi(page)
    const { prefix, made, victim, del } = await threeExpiring(page, 'c')
    const [, b, c] = made as [Made, Made, Made]
    try {
      await open(page, 'exp', prefix, made)
      await select(page, c)
      await deleteToFrozen(page, del)
      await finish(page, del, victim)
      await expectShown(page, b)
    }
    finally { await cleanup(page, made) }
  })

  for (const view of ['all', 'search'] as const) {
    const what = view === 'all' ? '«Все»' : 'поиск'
    test(`${what}, удаление среднего, в списке он сперва замороженный: выбран сосед снизу`, async ({ page }) => {
      await loginApi(page)
      const { prefix, made, victim, del } = await threeExpiring(page, 'b')
      const [, b, c] = made as [Made, Made, Made]
      try {
        await open(page, view, prefix, made)
        await select(page, b)
        await deleteToFrozen(page, del)
        await finish(page, del, victim)
        await expectShown(page, c)
      }
      finally { await cleanup(page, made) }
    })
  }
})
