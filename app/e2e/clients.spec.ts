import { expect, toast, uniq, loginApi, test } from './support/fixtures'
import { clientCard, clientName, createClientUi, nextYear, openClient, openHome, addDeviceButton, configDialog, deviceCard, ikeField } from './support/clients'
import { rects } from './support/geometry'
import { type Page, type Locator, test as baseTest } from '@playwright/test'
import { waitAccess } from './support/access'
import { container, inside } from './support/openvpn-ca'
import { proxiesBarrier as proxies } from './support/proxies-barrier'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as schema from '../server/database/schema'
import { serverModule } from './support/server-module'
import { noHorizontalOverflow } from './support/overflow'

test.describe('clients', () => {
  test.describe('клиенты', () => {
    test('«Отмена» в форме нового клиента ничего не создаёт', async ({ page }) => {
      await openHome(page, '/clients')
      const name = uniq('cancel')
      await page.getByRole('button', { name: 'Новый клиент' }).click()
      await page.getByPlaceholder('Иван Иванов').fill(name)
      await page.getByRole('button', { name: 'Отмена' }).click()
      await expect(page.getByRole('button', { name: 'Новый клиент' })).toBeVisible()
      const list: { name: string }[] = await (await page.request.get('/api/clients')).json()
      expect(list.map(c => c.name)).not.toContain(name)
    })

    test('создать, найти, переименовать, срок, заморозка, удалить', async ({ page }) => {
      await openHome(page, '/clients')
      const name = uniq('client')
      const expires = nextYear()
      const creating = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients')
      await createClientUi(page, name, { expires })
      const created = await creating
      expect(created.status()).toBe(200)
      const client = await created.json()
      expect(client.expiresAt).toBeTruthy()
      await page.getByPlaceholder('Поиск...').fill(name)
      await expect(clientCard(page, name)).toHaveCount(1)
      // Бейдж подписки — только «Заморожен»; у нового клиента вместо него точка «в сети».
      await expect(clientCard(page, name)).not.toContainText('Заморожен')
      await expect(clientCard(page, name).getByTestId('client-online')).toBeVisible()

      const dlg = await openClient(page, name)
      await expect(dlg.getByLabel('Истекает')).toHaveValue(expires)
      await expect(dlg.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()

      // Переименование по Enter.
      const renamed = `${name}-r`
      await clientName(dlg).fill(renamed)
      await clientName(dlg).press('Enter')
      await expect(toast(page, 'Имя сохранено')).toBeVisible()
      await expect(clientName(dlg)).toHaveText(renamed)
      await expect(clientCard(page, renamed)).toHaveCount(1)

      // Срок: очистить = бессрочно.
      const date = dlg.getByLabel('Истекает')
      // Сначала сервер завершает синхронизацию VPN, затем UI показывает toast.
      // Его короткое ожидание не должно включать всю длительность PATCH.
      const expirySaved = page.waitForResponse(r => r.request().method() === 'PATCH'
        && new URL(r.url()).pathname === `/api/clients/${client.id}`
        && r.request().postDataJSON()?.expiresAt === null)
      await date.fill('')
      await date.press('Tab')
      expect((await expirySaved).status()).toBe(202)
      await expect(toast(page, 'Срок действия сохранён')).toBeVisible()
      const saved = await page.request.get(`/api/clients/${client.id}`)
      expect(saved.status()).toBe(200)
      expect((await saved.json()).expiresAt, 'снятый срок должен сохраниться на сервере').toBeNull()
      await page.reload()
      await openClient(page, renamed)
      await expect(date).toHaveValue('')

      // Безлимит.
      await dlg.getByRole('button', { name: 'Безлимитно' }).click()
      await expect(dlg.getByText('лимит снят (∞)')).toBeVisible()

      // Заморозить и разморозить.
      await dlg.getByRole('button', { name: 'Заморозить' }).click()
      await expect(toast(page, 'Отключение выполняется')).toBeVisible()
      await expect(dlg.getByRole('status')).toContainText('Доступ отключён', { timeout: 45_000 })
      await expect(clientCard(page, renamed)).toContainText('Доступ отключён')
      await dlg.getByRole('button', { name: 'Разморозить' }).click()
      await expect(toast(page, 'Включение выполняется')).toBeVisible()
      await expect(dlg.getByRole('status')).toContainText('Доступ включён', { timeout: 45_000 })
      await expect(clientCard(page, renamed)).not.toContainText('Доступ отключён')

      // «Отмена» в подтверждениях.
      await dlg.getByRole('button', { name: 'Перевыпустить все ключи' }).click()
      const reissue = page.getByRole('dialog', { name: 'Перевыпустить все ключи?' })
      await reissue.getByRole('button', { name: 'Отмена' }).click()
      await expect(reissue).toBeHidden()

      await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
      const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
      await del.getByRole('button', { name: 'Отмена' }).click()
      await expect(del).toBeHidden()
      let list: { name: string }[] = await (await page.request.get('/api/clients')).json()
      expect(list.map(c => c.name)).toContain(renamed)

      await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
      await del.getByRole('button', { name: 'Удалить' }).click()
      await expect(toast(page, 'Отключение выполняется')).toBeVisible()
      await expect(page.getByRole('dialog')).toHaveCount(0)
      await expect(clientCard(page, renamed)).toHaveCount(0, { timeout: 45_000 })
      list = await (await page.request.get('/api/clients')).json()
      expect(list.map(c => c.name)).not.toContain(renamed)
    })

    // VPN2-69: 76 мест бросали код без текста, и тост, и бот показывали
    // «not_found», «invalid_id». Код остаётся в statusMessage, текст — из словаря.
    test('ошибки API без своего текста приходят по-русски, код в statusMessage', async ({ page, request }) => {
      await openHome(page, '/clients')
      const missing = await page.request.get('/api/clients/999999999')
      expect(missing.status()).toBe(404)
      expect(await missing.json()).toMatchObject({ statusMessage: 'not_found', message: 'Не найдено' })
      const bad = await page.request.get('/api/clients/abc')
      expect(bad.status()).toBe(400)
      expect(await bad.json()).toMatchObject({ statusMessage: 'invalid_id', message: 'Неверный идентификатор' })
      // Без сессии.
      const anon = await request.get('/api/clients')
      expect(anon.status()).toBe(401)
      expect(await anon.json()).toMatchObject({ statusMessage: 'Unauthorized', message: 'Нужно войти' })
    })
  })

  test.describe('рассылка под списком клиентов', () => {
    test('карточка рассылки стоит под списком клиентов', async ({ page }) => {
      await openHome(page, '/clients')
      const card = page.getByTestId('ov-broadcast')
      await expect(card.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toBeVisible()
      // В колонке списка ниже самого списка, не в карточке клиента.
      const [listBox, cardBox] = await rects(page, '.ccol .clist', '.ccol [data-testid="ov-broadcast"]')
      expect(cardBox.y).toBeGreaterThanOrEqual(listBox.y + listBox.height)
      await expect(page.locator('.ccol [data-testid="ov-broadcast"]')).toHaveCount(1)
      await expect(page.getByTestId('client-detail').getByTestId('ov-broadcast')).toHaveCount(0)
    })

    test('без настроенного бота рассылку не отправить, видна подсказка', async ({ page }) => {
      await page.route('**/api/admin/telegram', async (route) => {
        const res = await route.fetch()
        await route.fulfill({ response: res, json: { ...await res.json(), configured: false } })
      })
      await openHome(page, '/clients')
      const card = page.getByTestId('ov-broadcast')
      await card.getByLabel('Сообщение юзерам').fill('e2e без бота')
      await expect(card.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled()
      await expect(card.getByText('Сначала настройте Telegram-бота', { exact: true })).toBeVisible()
      // опрос может прийти под конец теста — не дать перехвату упасть уже в следующем
      await page.unrouteAll({ behavior: 'ignoreErrors' })
    })

    test('в шапке рассылки - @имя бота, пока бот его назвал', async ({ page }) => {
      let username = 'e2e_named_bot'
      await page.route('**/api/admin/telegram', async (route) => {
        const res = await route.fetch()
        await route.fulfill({ response: res, json: { ...await res.json(), configured: true, running: Boolean(username), username } })
      })
      await openHome(page, '/clients')
      const card = page.getByTestId('ov-broadcast')
      await expect(card.locator('.sh .tag')).toHaveText('@e2e_named_bot')
      username = ''
      await page.reload()
      await expect(card.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toBeVisible()
      await expect(card.locator('.sh .tag')).toHaveCount(0)
      await page.unrouteAll({ behavior: 'ignoreErrors' })
    })

    test('рассылка из-под списка клиентов уходит боту', async ({ page }) => {
      await openHome(page, '/clients')
      test.skip(!(await (await page.request.get('/api/admin/telegram')).json()).configured, 'нужна панель с ботом')
      const card = page.getByTestId('ov-broadcast')
      await expect(card.getByText('Сначала настройте Telegram-бота')).toHaveCount(0)
      const send = card.getByRole('button', { name: 'Отправить', exact: true })
      await expect(send).toBeDisabled()
      await card.getByLabel('Сообщение юзерам').fill('e2e рассылка со страницы клиентов')
      const posted = page.waitForRequest(r => r.url().endsWith('/api/admin/broadcast') && r.method() === 'POST')
      await send.click()
      expect((await posted).postDataJSON()).toEqual({ text: 'e2e рассылка со страницы клиентов' })
      await expect(card.getByLabel('Сообщение юзерам')).toHaveValue('')
    })
  })
})

test.describe('client-draft', () => {
  async function clientId(page: Page, name: string) {
    const list: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
    return list.find(c => c.name === name)!.id
  }

  for (const width of [1400, 390]) {
    test.describe(`черновик клиента, ${width}px`, () => {
      test.use({ viewport: { width, height: 844 } })

      test('409 при переименовании сохраняет введённое имя для исправления', async ({ page, guard }) => {
        await openHome(page, '/clients')
        const name = uniq('draft')
        const taken = uniq('taken')
        await createClientUi(page, name)
        await createClientUi(page, taken)
        const dialog = await openClient(page, name)
        const input = clientName(dialog)
        guard.allow(409, /\/api\/clients\/\d+$/, 'PATCH')
        await input.fill(taken)
        await input.press('Enter')
        await expect(toast(page, `Клиент с именем «${taken}» уже существует`)).toBeVisible()
        await expect(input).toHaveText(taken)
        // Карточка опрашивается каждые 3 с: свежий ответ сервера не затирает черновик.
        const id = await clientId(page, name)
        await page.waitForResponse(r => r.request().method() === 'GET' && new URL(r.url()).pathname === `/api/clients/${id}`)
        await page.evaluate(() => new Promise(done => setTimeout(() => requestAnimationFrame(() => done(null)), 50)))
        await expect(input).toHaveText(taken)
        const kept: { name: string } = await (await page.request.get(`/api/clients/${id}`)).json()
        expect(kept.name).toBe(name)
        await input.fill(`${taken}-fixed`)
        await input.press('Enter')
        await expect(toast(page, 'Имя сохранено')).toBeVisible()
        await expect(input).toHaveText(`${taken}-fixed`)
      })

      test('422 при сохранении срока сохраняет дату для исправления', async ({ page, guard }) => {
        await openHome(page, '/clients')
        const name = uniq('expiry-draft')
        await createClientUi(page, name)
        const dialog = await openClient(page, name)
        const input = dialog.getByLabel('Истекает')
        guard.allow(422, /\/api\/clients\/\d+$/, 'PATCH')
        await input.fill('01012000')
        await input.press('Tab')
        await expect(toast(page, 'Срок действия не может быть раньше завтрашней даты')).toBeVisible()
        await expect(input).toHaveValue('01.01.2000')
        await input.fill(nextYear().replace(/\D/g, ''))
        await input.press('Tab')
        await expect(toast(page, 'Срок действия сохранён')).toBeVisible()
      })
    })
  }
})

test.describe('client-gone', () => {
  /**
   * Клиента удаляет второй админ (или тест через API), пока панель грузит его
   * карточку: GET карточки и трафика приходят 404. Панель молча убирает его из
   * выбора и показывает следующего, без тоста об ошибке.
   */

  /** Два своих клиента; список на странице сужен до них, первым выбирается first. */
  async function twoClients(page: Page) {
    const prefix = uniq('gone')
    const ids: number[] = []
    for (const suffix of ['a', 'b']) {
      const r = await page.request.post('/api/clients', { data: { name: `${prefix}-${suffix}`, deviceLimit: null } })
      expect(r.status(), await r.text()).toBe(200)
      ids.push((await r.json()).id)
    }
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const response = await route.fetch()
      const list: { name: string }[] = await response.json()
      // Новый опрос списка обрывает прежний запрос (useFetch отменяет его): отвечать уже некому.
      await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    return { first: { id: ids[0]!, name: `${prefix}-a` }, second: { id: ids[1]!, name: `${prefix}-b` } }
  }

  function toasts(page: Page) {
    return page.getByRole('region', { includeHidden: true }).locator('ol > li')
  }

  test('клиента удалили до загрузки карточки: панель молча выбирает следующего', async ({ page }) => {
    await loginApi(page)
    const { first, second } = await twoClients(page)
    try {
      const gone: string[] = []
      page.on('response', (r) => {
        if (r.status() === 404 && r.url().includes(`/api/clients/${first.id}`)) gone.push(r.url())
      })
      // Первый запрос карточки выбранного клиента: удалить его и
      // только потом пропустить запрос к серверу — он ответит настоящим 404.
      let deleted: Promise<unknown> | null = null
      await page.route(new RegExp(`/api/clients/${first.id}$`), async (route) => {
        deleted ??= page.request.delete(`/api/clients/${first.id}`).then(async r => waitAccess(page.request, (await r.json()).operation))
        await deleted
        await route.continue()
      })

      await page.goto('/clients')
      const panel = page.getByTestId('client-detail')
      await expect(clientName(panel)).toHaveText(second.name)
      // Без повторов: тост гаснет сам, ожидание нуля его бы пересидело.
      expect(await toasts(page).allInnerTexts(), 'тост при пропавшем клиенте').toEqual([])
      await expect(clientCard(page, second.name)).toHaveAttribute('aria-pressed', 'true')
      await expect(clientCard(page, first.name)).toHaveCount(0)
      await page.waitForLoadState('networkidle')
      // Положительный контроль: гонка действительно случилась.
      expect(gone.length, 'карточка удалённого клиента не запрашивалась').toBeGreaterThan(0)
      expect(await toasts(page).allInnerTexts(), 'тост при пропавшем клиенте').toEqual([])
    }
    finally {
      await page.unrouteAll({ behavior: 'wait' })
      await page.request.delete(`/api/clients/${first.id}`)
      await page.request.delete(`/api/clients/${second.id}`)
    }
  })

  test('удаление выбранного клиента из своей панели не запрашивает его снова', async ({ page }) => {
    await loginApi(page)
    const { first, second } = await twoClients(page)
    try {
      const missing: string[] = []
      let lists = 0
      page.on('response', (r) => {
        if (r.status() === 404) missing.push(`${r.request().method()} ${r.url()}`)
        if (r.request().method() === 'GET' && /\/api\/clients(\?.*)?$/.test(r.url())) lists++
      })
      await page.goto('/clients')
      const panel = page.getByTestId('client-detail')
      await expect(clientName(panel)).toHaveText(first.name)
      await panel.getByRole('button', { name: 'Удалить клиента' }).click()
      await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
      await expect(clientName(panel)).toHaveText(second.name)
      await expect(clientCard(page, first.name)).toHaveCount(0)
      // Не networkidle: список опрашивается каждые 3 с, а запрос, оборванный новым опросом
      // внутри page.route, остаётся в сети до конца теста. Два ответа списка после удаления -
      // полный цикл опроса, за который панель запросила бы удалённого клиента снова.
      const listed = lists
      await expect.poll(() => lists - listed, { timeout: 15_000 }).toBeGreaterThanOrEqual(2)
      expect(missing).toEqual([])
    }
    finally {
      await page.unrouteAll({ behavior: 'wait' })
      await page.request.delete(`/api/clients/${first.id}`)
      await page.request.delete(`/api/clients/${second.id}`)
    }
  })

  test('удаление после опроса карточки при открытом диалоге не запрашивает удалённого клиента', async ({ page }) => {
    await loginApi(page)
    const { first, second } = await twoClients(page)
    try {
      const missing: string[] = []
      let lists = 0
      page.on('response', (r) => {
        if (r.status() === 404 && r.url().includes(`/api/clients/${first.id}`)) missing.push(`${r.request().method()} ${r.url()}`)
        if (r.request().method() === 'GET' && /\/api\/clients(\?.*)?$/.test(r.url())) lists++
      })
      // Карточку удаляемого, запрошенную после DELETE, сервер отдаёт до подтверждения
      // удаления, а после — 404. Такой запрос держится до подтверждения, чтобы
      // лишний GET всегда получал 404, как на медленной сети.
      let confirmed: Promise<unknown> | null = null
      await page.route(new RegExp(`/api/clients/${first.id}$`), async (route) => {
        const method = route.request().method()
        if (method === 'DELETE') {
          const response = await route.fetch()
          const { operation } = await response.json()
          confirmed = waitAccess(page.request, operation)
          return route.fulfill({ response })
        }
        if (method === 'GET' && confirmed) await confirmed
        await route.fallback()
      })
      await page.goto('/clients')
      const panel = page.getByTestId('client-detail')
      await expect(clientName(panel)).toHaveText(first.name)
      await panel.getByRole('button', { name: 'Удалить клиента' }).click()
      const dialog = page.getByRole('dialog', { name: 'Удалить клиента?' })
      await expect(dialog).toBeVisible()
      // Пока диалог открыт, опрос карточки подменяет её новым объектом.
      await page.waitForResponse(r => r.request().method() === 'GET' && new RegExp(`/api/clients/${first.id}$`).test(r.url()) && r.ok())
      await dialog.getByRole('button', { name: 'Удалить' }).click()
      await expect(clientName(panel)).toHaveText(second.name, { timeout: 45_000 })
      await expect(clientCard(page, first.name)).toHaveCount(0)
      // Не networkidle: список опрашивается каждые 3 с, а запрос, оборванный новым опросом
      // внутри page.route, остаётся в сети до конца теста. Удержанный GET отпускается
      // подтверждением; два ответа списка после него - полный цикл опроса.
      await confirmed
      const listed = lists
      await expect.poll(() => lists - listed, { timeout: 15_000 }).toBeGreaterThanOrEqual(2)
      expect(missing).toEqual([])
    }
    finally {
      await page.unrouteAll({ behavior: 'wait' })
      await page.request.delete(`/api/clients/${first.id}`)
      await page.request.delete(`/api/clients/${second.id}`)
    }
  })

  test('запоздалый список после удаления не выбирает удалённого клиента снова', async ({ page }) => {
    await loginApi(page)
    const { first, second } = await twoClients(page)
    try {
      const missing: string[] = []
      page.on('response', (r) => {
        if (r.status() === 404) missing.push(`${r.request().method()} ${r.url()}`)
      })
      // Удаление подтвердилось, а ответ списка, начатого раньше, ещё несёт
      // удалённого клиента. Списки с DELETE до подтверждения и 4 с после
      // отвечают снимком до удаления: подтверждение замечает карточка клиента,
      // а страница в этом окне видит старый список.
      let snapshot: unknown = null
      let deleting = false
      let confirmedAt = 0
      page.on('response', async (r) => {
        if (!deleting && r.request().method() === 'GET' && /\/api\/clients(\?.*)?$/.test(r.url()) && r.ok()) snapshot = await r.json().catch(() => snapshot)
      })
      await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
        const response = await route.fetch()
        const operation: { status: string } = await response.json()
        if (operation.status === 'confirmed') confirmedAt ||= Date.now()
        await route.fulfill({ response })
      })
      await page.route(new RegExp(`/api/clients/${first.id}$`), async (route) => {
        if (route.request().method() === 'DELETE') deleting = true
        await route.fallback()
      })
      await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
        const stale = deleting && (!confirmedAt || Date.now() < confirmedAt + 4_000)
        if (route.request().method() !== 'GET' || !stale) return route.fallback()
        await route.fulfill({ json: snapshot }).catch((e: Error) => {
          if (!e.message.includes('already handled')) throw e
        })
      })
      await page.goto('/clients')
      const panel = page.getByTestId('client-detail')
      await expect(clientName(panel)).toHaveText(first.name)
      await panel.getByRole('button', { name: 'Удалить клиента' }).click()
      await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
      await expect.poll(() => confirmedAt > 0, { timeout: 45_000 }).toBe(true)
      await expect(clientName(panel)).toHaveText(second.name)
      await expect(clientCard(page, first.name)).toHaveCount(0, { timeout: 15_000 })
      await page.waitForLoadState('networkidle')
      expect(missing).toEqual([])
      await expect(clientName(panel)).toHaveText(second.name)
    }
    finally {
      await page.unrouteAll({ behavior: 'wait' })
      await page.request.delete(`/api/clients/${first.id}`)
      await page.request.delete(`/api/clients/${second.id}`)
    }
  })
})

test.describe('client-detail-race', () => {
  for (const width of [1400, 390]) {
    test.describe(`переключение клиента, ${width}px`, () => {
      test.use({ viewport: { width, height: 844 } })

      test('запоздавший GET прежнего клиента не подменяет выбранного', async ({ page }) => {
        await openHome(page, '/clients')
        const first = uniq('first')
        const second = uniq('second')
        await createClientUi(page, first)
        await createClientUi(page, second)
        const clients: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
        const id = clients.find(c => c.name === first)!.id
        let release!: () => void
        const held = new Promise<void>((resolve) => { release = resolve })
        let fetched!: () => void
        const ready = new Promise<void>((resolve) => { fetched = resolve })
        await page.route(`**/api/clients/${id}`, async (route) => {
          const response = await route.fetch()
          fetched()
          await held
          await route.fulfill({ response })
        })
        const panel = page.getByTestId('client-detail')
        try {
          await page.getByPlaceholder('Поиск...').fill(first)
          await clientCard(page, first).click()
          await ready
          // До 720px карточка — отдельный экран: к списку возвращает «Назад».
          if (width <= 720) await page.getByRole('button', { name: 'Назад' }).click()
          await openClient(page, second)
        }
        finally {
          release()
        }
        await page.unrouteAll({ behavior: 'wait' })
        // Дать браузеру обработать освобождённый ответ и следующую отрисовку.
        await page.waitForLoadState('networkidle')
        await expect(clientName(panel)).toHaveText(second)
        await expect(clientCard(page, second)).toHaveAttribute('aria-pressed', 'true')
      })
    })
  }

  test.describe('переключение клиента, ввод имени до загрузки нового', () => {
    test.use({ viewport: { width: 1440, height: 900 } })

    test('VPN2-524: пока грузится карточка B, имя клиента A не правится и не сохраняется', async ({ page }) => {
      await openHome(page, '/clients')
      const a = uniq('a')
      const b = uniq('b')
      await createClientUi(page, a)
      await createClientUi(page, b)
      const clients: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
      const idA = clients.find(c => c.name === a)!.id
      const idB = clients.find(c => c.name === b)!.id
      const panel = await openClient(page, a)
      const patches: string[] = []
      page.on('request', (r) => { if (r.method() === 'PATCH') patches.push(r.url()) })
      let release!: () => void
      const held = new Promise<void>((resolve) => { release = resolve })
      let fetched!: () => void
      const ready = new Promise<void>((resolve) => { fetched = resolve })
      await page.route(`**/api/clients/${idB}`, async (route) => {
        if (route.request().method() !== 'GET') return route.fallback()
        const response = await route.fetch()
        fetched()
        await held
        await route.fulfill({ response })
      })
      try {
        await page.getByPlaceholder('Поиск...').fill(b)
        await clientCard(page, b).click()
        await ready
        // Карточка B ещё не пришла: на экране A. Человек ставит курсор в имя и вводит новое.
        await expect(clientName(panel)).toHaveText(a)
        await clientName(panel).focus()
        await page.keyboard.type('-renamed')
        await page.keyboard.press('Enter')
      }
      finally {
        release()
      }
      await page.unrouteAll({ behavior: 'wait' })
      await expect(clientName(panel)).toHaveText(b)
      await page.waitForLoadState('networkidle')
      expect(patches.filter(u => new RegExp(`/api/clients/${idA}$`).test(u))).toEqual([])
      const after: { id: number, name: string } = await (await page.request.get(`/api/clients/${idA}`)).json()
      expect(after.name).toBe(a)
    })
  })
})

test.describe('client-name-race', () => {
  test('имя проверяется при переименовании после параллельного создания клиента', async ({ page }) => {
    test.skip(!container, 'барьер sing-box доступен только в одноразовом e2e-контейнере')
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('rename-race') } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    const url = `/api/clients/${client.id}`
    const name = uniq('claimed-name')
    try {
      proxies('POST', { clientName: name })
      const result = await page.request.patch(url, { data: { name, preferredExit: 'nl' } })
      expect(proxies('GET').insertedId, 'конкурентная запись выполнена после первой проверки имени').not.toBeNull()
      const all: Array<{ name: string }> = await (await page.request.get('/api/clients')).json()
      const matching = all.filter(client => client.name === name).length
      console.log(`rename race: PATCH=${result.status()}, clients with requested name=${matching}`)
      expect(result.status()).toBe(409)
      expect(matching).toBe(1)
      expect(await result.json()).toMatchObject({ statusMessage: 'client_exists' })
      const after = await (await page.request.get(url)).json()
      expect(after.name).toBe(client.name)
      expect(after.preferredExit).toBeNull()
    }
    finally {
      const otherId = proxies('DELETE').insertedId
      await page.request.delete(url)
      if (otherId) await page.request.delete(`/api/clients/${otherId}`)
    }
  })
})

test.describe('name-validation', () => {
  test('пустое после trim имя клиента отклоняется при создании', async ({ page }) => {
    await loginApi(page)
    const response = await page.request.post('/api/clients', { data: { name: ' \t\n ' } })
    // В старой версии клиент уже создан: убираем его, хотя имя не имеет e2e-префикса.
    if (response.ok()) {
      const client = await response.json()
      await page.request.delete(`/api/clients/${client.id}`)
    }
    expect(response.status()).toBe(400)
  })

  test('пустое после trim имя не стирает имя существующего клиента', async ({ page }) => {
    await loginApi(page)
    const name = uniq('name-validation')
    const created = await page.request.post('/api/clients', { data: { name } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    try {
      const response = await page.request.patch(`/api/clients/${client.id}`, { data: { name: '\u00a0\u2003' } })
      expect(response.status()).toBe(400)
      expect((await (await page.request.get(`/api/clients/${client.id}`)).json()).name).toBe(name)
    }
    finally {
      await page.request.delete(`/api/clients/${client.id}`)
    }
  })

  test('пустое после trim имя устройства не создаёт запись и ключи', async ({ page }) => {
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('device-name') } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    try {
      const response = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: '   ' } })
      expect(response.status()).toBe(400)
      expect((await (await page.request.get(`/api/clients/${client.id}`)).json()).devices).toEqual([])
      const valid = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: ' phone ' } })
      expect(valid.status()).toBe(200)
      expect((await valid.json()).name).toBe('phone')
    }
    finally {
      await page.request.delete(`/api/clients/${client.id}`)
    }
  })
})

test.describe('client-create-filtered', () => {
  /**
   * Новый клиент после создания выбран и виден строкой в списке: поиск и фильтр,
   * которые его скрывают, сбрасываются; не скрывают - остаются как были.
   */

  interface Made { id: number, name: string }

  /** Свой клиент `<префикс>-<суффикс>`; список на странице сужен до префикса. */
  async function ownClients(page: Page, prefix: string, suffixes: string[]) {
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

  /** Создать клиента формой; вернуть его вместе с id из ответа. */
  async function create(page: Page, name: string): Promise<Made> {
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients'),
      createClientUi(page, name),
    ])
    return { id: (await response.json()).id, name }
  }

  /** Новый выбран: карточка его, строка видна и подсвечена. */
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

  const search = (page: Page) => page.getByPlaceholder('Поиск...')
  const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

  test('создание при поиске, который нового не находит: поиск сброшен, новый выбран и виден', async ({ page }) => {
    await loginApi(page)
    const prefix = uniq('cf')
    const made = await ownClients(page, prefix, ['a-keep'])
    try {
      await page.goto('/clients')
      await search(page).fill('keep')
      await expect(clientCard(page, made[0]!.name)).toBeVisible()
      made.push(await create(page, `${prefix}-b-new`))
      await expectShown(page, made[1]!)
      await expect(search(page)).toHaveValue('')
    }
    finally { await cleanup(page, made) }
  })

  test('создание при фильтре «Заморожены»: фильтр сброшен, новый выбран и виден', async ({ page }) => {
    await loginApi(page)
    const prefix = uniq('cf')
    const made = await ownClients(page, prefix, ['a'])
    try {
      const r = await page.request.patch(`/api/clients/${made[0]!.id}`, { data: { frozenManual: true } })
      expect(r.status(), await r.text()).toBe(202)
      await waitAccess(page.request, (await r.json()).operation)
      await page.goto('/clients')
      await segment(page, 'Заморожены').click()
      await expect(clientCard(page, made[0]!.name)).toBeVisible()
      made.push(await create(page, `${prefix}-b`))
      await expectShown(page, made[1]!)
      await expect(segment(page, 'Все')).toHaveClass(/\bon\b/)
    }
    finally { await cleanup(page, made) }
  })

  test('создание при поиске, который нового находит: поиск остался, новый выбран и виден', async ({ page }) => {
    await loginApi(page)
    const prefix = uniq('cf')
    const made = await ownClients(page, prefix, ['a-keep', 'c-hide'])
    try {
      await page.goto('/clients')
      await search(page).fill('keep')
      await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
      made.push(await create(page, `${prefix}-b-keep`))
      await expectShown(page, made[2]!)
      await expect(search(page)).toHaveValue('keep')
      await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
    }
    finally { await cleanup(page, made) }
  })
})

test.describe('create-concurrency', () => {
  test('параллельное создание и повтор запроса не дублируют клиента и устройство', async ({ page }) => {
    await loginApi(page)
    const name = uniq('create-race')
    const created = await Promise.all(Array.from({ length: 16 }, () =>
      page.request.post('/api/clients', { data: { name } }),
    ))
    expect(created.map(r => r.status()).sort()).toEqual([200, ...Array(15).fill(409)])
    const client = await created.find(r => r.status() === 200)!.json()
    const url = `/api/clients/${client.id}`
    try {
      expect((await page.request.post('/api/clients', { data: { name } })).status()).toBe(409)
      const added = await Promise.all(Array.from({ length: 16 }, () =>
        page.request.post(`${url}/devices`, { data: { name: 'phone' } }),
      ))
      expect(added.map(r => r.status()).sort()).toEqual([200, ...Array(15).fill(409)])
      expect((await page.request.post(`${url}/devices`, { data: { name: 'phone' } })).status()).toBe(409)
      const after = await (await page.request.get(url)).json()
      expect(after.devices).toHaveLength(1)
      console.log('create/retry: clients 1 accepted + 15 conflicts; devices 1 accepted + 15 conflicts; both retries 409')
    }
    finally {
      await page.request.delete(url)
    }
  })

  test('параллельная выдача адресов всех протоколов не создаёт коллизий', async ({ page }) => {
    test.skip(!container, 'чтение SQLite разрешено только в одноразовом e2e-контейнере')
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('ip-race'), deviceLimit: null } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    const url = `/api/clients/${client.id}`
    try {
      const added = await Promise.all(Array.from({ length: 16 }, (_, i) =>
        page.request.post(`${url}/devices`, { data: { name: `phone-${i}` } }),
      ))
      expect(added.map(r => r.status())).toEqual(Array(16).fill(200))
      const rows: Array<Record<string, string | null>> = JSON.parse(inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec('PRAGMA busy_timeout = 10000');
      console.log(JSON.stringify(db.prepare('SELECT wg_ip, ovpn_ip, ikev2_ip, ikev2_username FROM devices WHERE client_id = ?').all(JSON.parse(process.argv[1]))));
      db.close();
    `, client.id))
      expect(rows).toHaveLength(16)
      for (const field of ['wg_ip', 'ovpn_ip', 'ikev2_ip', 'ikev2_username']) {
        const values = rows.map(row => row[field])
        expect(values.every(Boolean), field).toBe(true)
        expect(new Set(values).size, field).toBe(16)
      }
      console.log('address claims: 16 accepted; 16 unique WG/OVPN/IKEv2 addresses and IKEv2 usernames')
    }
    finally {
      await page.request.delete(url)
    }
  })

  test('параллельные новые устройства соблюдают лимит клиента', async ({ page }) => {
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('create-limit'), deviceLimit: 1 } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    const url = `/api/clients/${client.id}`
    try {
      const added = await Promise.all(Array.from({ length: 16 }, (_, i) =>
        page.request.post(`${url}/devices`, { data: { name: `phone-${i}` } }),
      ))
      expect(added.map(r => r.status()).sort()).toEqual([200, ...Array(15).fill(409)])
      expect((await (await page.request.get(url)).json()).devices).toHaveLength(1)
      console.log('create/limit: 1 accepted + 15 conflicts; devices=1, limit=1')
    }
    finally {
      await page.request.delete(url)
    }
  })
})

test.describe('client-prefs', () => {
  const test = baseTest

  for (const scenario of [
    { name: 'переименование', hosts: 'se1=192.0.2.2,us=192.0.2.3', expected: ['se1', 'us'] },
    { name: 'обмен тегами', hosts: 'us=192.0.2.2,se=192.0.2.3', expected: ['us', 'se'] },
    { name: 'замена адреса при том же теге', hosts: 'se=192.0.2.4,us=192.0.2.3', expected: ['se', 'us'] },
    { name: 'удаление экзита', hosts: 'us=192.0.2.3', expected: [null, 'us'] },
    { name: 'удаление и новый адрес с новым тегом', hosts: 'se1=192.0.2.4,us=192.0.2.3', expected: [null, 'us'] },
    { name: 'неоднозначный адрес', hosts: 'se1=192.0.2.2,se2=192.0.2.2,us=192.0.2.3', expected: [null, 'us'] },
    { name: 'тег constructor', hosts: 'constructor=192.0.2.2,us=192.0.2.3', expected: ['constructor', 'us'] },
    { name: 'удаление тега constructor', initial: ['constructor', 'us'], hosts: 'us=192.0.2.3', expected: [null, 'us'] },
  ]) {
    const initial = scenario.initial ?? ['se', 'us']
    const before = `ru=192.0.2.1,${initial[0]}=192.0.2.2,us=192.0.2.3`
    for (const upgrade of [false, true]) {
      test(`предпочтения: ${scenario.name}${upgrade ? ', обновление старой панели' : ''}`, async () => {
        const directory = await mkdtemp(join(tmpdir(), 'vpn2-client-prefs-'))
        const client = createClient({ url: ':memory:' })
        const db = drizzle(client, { schema })
        const cfg = { exitTags: initial.join(' '), nodeHosts: before, previousNodeHosts: '', clientPrefsFilePath: join(directory, 'prefs.json') }
        const logs: { oldTag: string, newTag: string | null }[] = []
        const load = () => serverModule<typeof import('../server/utils/client-prefs')>(
          new URL('../server/utils/client-prefs.ts', import.meta.url), {
            '../database/client': { useDb: () => db }, '../database/schema': schema,
            './control': { controlApply: async (_method: string, params: { config: unknown }) => {
              await writeFile(cfg.clientPrefsFilePath, JSON.stringify(params.config))
            } },
          }, { process, useRuntimeConfig: () => cfg, useLogger: () => ({ info: (data: { oldTag: string, newTag: string | null }) => { if (data.oldTag) logs.push(data) } }) },
        )
        try {
          await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
          for (const [i, tag] of [...initial, null].entries()) {
            const [owner] = await db.insert(schema.clients).values({ name: `client-${i}`, password: 'unused-test-password', preferredExit: tag }).returning()
            await db.insert(schema.devices).values({ clientId: owner!.id, name: 'device', wgIp: `10.66.66.${i + 2}`, ovpnIp: `10.67.67.${i + 2}`, ikev2Ip: `10.68.68.${i + 2}` })
          }
          if (upgrade) cfg.previousNodeHosts = before
          else await load().syncClientPrefsFile()
          cfg.nodeHosts = scenario.hosts
          cfg.exitTags = scenario.hosts.split(',').map(pair => pair.split('=')[0]).join(' ')
          if (!upgrade) {
            // Ошибка сохранения карты должна откатить и переносы клиентов.
            await client.execute("CREATE TRIGGER fail_map BEFORE INSERT ON collector_state BEGIN SELECT RAISE(ABORT, 'disk is full'); END")
            await expect(Promise.resolve(load().syncClientPrefsFile())).rejects.toThrow()
            expect((await db.select().from(schema.clients).orderBy(schema.clients.id)).map(row => row.preferredExit)).toEqual([...initial, null])
            expect(logs).toHaveLength(0)
            await client.execute('DROP TRIGGER fail_map')
          }
          await load().syncClientPrefsFile()
          const actual = (await db.select().from(schema.clients).orderBy(schema.clients.id)).map(row => row.preferredExit)
          console.log(`${scenario.name}: ${JSON.stringify([...initial, null])} → ${JSON.stringify(actual)}`)
          expect(actual).toEqual([...scenario.expected, null])
          const prefs = JSON.parse(await readFile(cfg.clientPrefsFilePath, 'utf8'))
          for (const [i, tag] of scenario.expected.entries()) {
            if (tag) expect(prefs[tag]).toEqual([`10.66.66.${i + 2}`, `10.67.67.${i + 2}`, `10.68.68.${i + 2}`])
          }
          expect(Object.keys(prefs).sort()).toEqual(scenario.expected.filter(Boolean).sort())
          const changed = scenario.expected.filter((tag, i) => tag !== initial[i]).length
          expect(logs).toHaveLength(changed)
          // Рестарт с прежним bootstrap-env не переносит уже перенесённых клиентов повторно.
          await load().syncClientPrefsFile()
          expect((await db.select().from(schema.clients).orderBy(schema.clients.id)).map(row => row.preferredExit)).toEqual(actual)
          expect(logs).toHaveLength(changed)
        }
        finally {
          client.close()
          await rm(directory, { recursive: true, force: true })
        }
      })
    }
  }
})

test.describe('mobile-clients', { tag: '@mobile' }, () => {
  /** Поле срока сразу после открытия «Нового клиента»: автофокус окна не уводит набор в «Имя». */
  async function typeExpiryRightAway(page: Page, tap: boolean) {
    await openHome(page, '/clients')
    const date = nextYear()
    const name = `Срок ${Date.now()}`
    const create = page.getByRole('button', { name: 'Новый клиент' })
    if (tap) await create.tap()
    else await create.click()
    const dlg = page.getByRole('dialog', { name: 'Новый клиент' })
    const expiry = dlg.getByPlaceholder('ДД.ММ.ГГГГ')
    await expiry.focus()
    await expiry.pressSequentially(date.replace(/\D/g, ''), { delay: 40 })
    await expect(expiry).toHaveValue(date)
    await expect(dlg.getByPlaceholder('Иван Иванов')).toHaveValue('')

    await dlg.getByPlaceholder('Иван Иванов').fill(name)
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients'),
      dlg.getByRole('button', { name: 'Создать' }).click(),
    ])
    expect(response.status()).toBe(200)
    const [d, m, y] = date.split('.')
    expect(response.request().postDataJSON().expiresAt).toContain(`${y}-${m}-${d}`)
  }

  test.describe('десктоп', () => {
    // Файл в проекте mobile: окно — как у проекта desktop.
    test.use({ viewport: { width: 1400, height: 1000 }, isMobile: false, hasTouch: false, deviceScaleFactor: 1 })

    test('срок, набранный сразу после открытия окна, не теряет цифр', async ({ page }) => {
      await typeExpiryRightAway(page, false)
    })
  })

  /** Длинное имя: 64 знака, предел сервера; префикс e2e- снесёт teardown. */
  function longName() {
    return `${uniq('m')} Константин Длиннофамильный-Ласточкин-Задунайский`.slice(0, 64)
  }

  /** Строка списка в одну линию: имя и мета обрезаются, правый столбец не съезжает. */
  async function rowFits(row: Locator) {
    const m = await row.evaluate((el) => {
      const r = el.getBoundingClientRect()
      const who = el.querySelector('.who>div')!.getBoundingClientRect()
      const right = el.querySelector('.cxp')!.getBoundingClientRect()
      const name = el.querySelector('.who b')!.getBoundingClientRect()
      return { h: r.height, right: r.right, vw: document.documentElement.clientWidth, gap: right.left - who.right, nameH: name.height }
    })
    expect(m.right, 'строка вылезла за экран').toBeLessThanOrEqual(m.vw)
    expect(m.gap, 'имя наехало на срок').toBeGreaterThanOrEqual(0)
    expect(m.nameH, 'имя переносится на вторую строку').toBeLessThan(24)
    expect(m.h, 'строка выросла').toBeLessThan(70)
  }

  /** Кнопки шторки целиком на экране, ничего не уходит вбок. */
  async function sheetFits(page: Page, dlg: Locator, where: string) {
    await expect(dlg).toBeVisible()
    await noHorizontalOverflow(page, where)
    const vw = page.viewportSize()!.width
    for (const b of await dlg.locator('.mf .pill').all()) {
      const box = (await b.boundingBox())!
      expect(box.x, `${where}: кнопка за левым краем`).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width, `${where}: кнопка за правым краем`).toBeLessThanOrEqual(vw)
    }
  }

  async function closeSheet(dlg: Locator) {
    await dlg.getByRole('button', { name: 'Закрыть' }).tap()
    await expect(dlg).toBeHidden()
  }

  async function tapDownload(page: Page, dlg: Locator, name: string) {
    const [d] = await Promise.all([page.waitForEvent('download'), dlg.getByRole('button', { name, exact: true }).tap()])
    return d.suggestedFilename()
  }

  for (const width of [360, 402]) {
    test.describe(`телефон ${width}`, () => {
      test.use({ viewport: { width, height: 874 } })

      test('срок, набранный сразу после открытия шторки, не теряет цифр', async ({ page }) => {
        await typeExpiryRightAway(page, true)
      })

      for (const scheme of ['dark', 'light'] as const) {
        test(`тема ${scheme}: клиент от создания до удаления, девайс и все три конфига`, async ({ page }) => {
          await page.emulateMedia({ colorScheme: scheme })
          await openHome(page, '/clients')
          await noHorizontalOverflow(page, 'список')

          const name = longName()
          await page.getByRole('button', { name: 'Новый клиент' }).tap()
          const create = page.getByRole('dialog', { name: 'Новый клиент' })
          await create.getByPlaceholder('Иван Иванов').fill(name)
          await sheetFits(page, create, 'шторка нового клиента')
          await create.getByRole('button', { name: 'Создать' }).tap()
          await expect(toast(page, `Клиент «${name}» создан`)).toBeVisible()
          await expect(create).toBeHidden()

          // Поиск и фильтры.
          const row = clientCard(page, name)
          await page.getByPlaceholder('Поиск...').fill(name)
          await expect(row).toHaveCount(1)
          await rowFits(row)
          const seg = page.locator('.clp .seg')
          await seg.getByRole('button', { name: /^Заморожены/ }).tap()
          await expect(row).toHaveCount(0)
          await seg.getByRole('button', { name: /^Все/ }).tap()
          await expect(row).toHaveCount(1)

          // Карточка клиента — отдельный экран вместо списка.
          await row.tap()
          const panel = page.getByTestId('client-detail')
          await expect(clientName(panel)).toHaveText(name)
          await expect(row).toBeHidden()
          await noHorizontalOverflow(page, 'карточка клиента')

          const expires = nextYear()
          const expiry = panel.getByLabel('Истекает')
          await expiry.fill(expires)
          await expiry.press('Tab')
          await expect(toast(page, 'Срок действия сохранён')).toBeVisible()
          await panel.getByRole('button', { name: 'Безлимитно' }).tap()
          await expect(panel.getByText('лимит снят (∞)')).toBeVisible()
          await panel.getByLabel('Предпочитаемый экзит').selectOption('fi')
          await expect(toast(page, 'Предпочитаемый экзит: FI')).toBeVisible()

          // Девайс и конфиги.
          const device = 'phone'
          await addDeviceButton(page).tap()
          const add = page.getByRole('dialog', { name: 'Новый девайс' })
          await add.getByPlaceholder('напр. iPhone').fill(device)
          await sheetFits(page, add, 'шторка нового девайса')
          await add.getByRole('button', { name: 'Добавить' }).tap()
          await expect(toast(page, `Девайс «${device}» добавлен`)).toBeVisible({ timeout: 30_000 })
          await expect(add).toBeHidden()
          const dc = deviceCard(page, device)
          await noHorizontalOverflow(page, 'карточка с девайсом')

          await dc.wg.tap()
          const wg = configDialog(page, device, 'WireGuard')
          const qr = wg.locator('img[alt="WireGuard QR"]')
          await expect(qr).toBeInViewport()
          await expect.poll(() => qr.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0)
          await sheetFits(page, wg, 'шторка WireGuard')
          await wg.getByRole('button', { name: 'Копировать', exact: true }).tap()
          await expect(toast(page, '.conf скопирован')).toBeVisible()
          expect(await tapDownload(page, wg, 'Скачать')).toMatch(/\.conf$/)
          await expect(wg.getByRole('button', { name: 'В TG', exact: true })).toBeInViewport()
          await closeSheet(wg)

          await dc.ovpn.tap()
          const ovpn = configDialog(page, device, 'OpenVPN')
          await expect(ovpn.getByText('генерирую сертификат…')).toBeHidden({ timeout: 30_000 })
          await sheetFits(page, ovpn, 'шторка OpenVPN')
          expect(await tapDownload(page, ovpn, 'Скачать')).toMatch(/\.ovpn$/)
          await closeSheet(ovpn)

          await dc.ikev2.tap()
          const ike = configDialog(page, device, 'IKEv2')
          await expect(ikeField(ike, 'Сервер')).not.toBeEmpty({ timeout: 30_000 })
          await sheetFits(page, ike, 'шторка IKEv2')
          await ike.locator('.mf').getByRole('button', { name: 'Копировать', exact: true }).tap()
          await expect(toast(page, 'IKEv2 креды скопированы')).toBeVisible()
          await closeSheet(ike)

          await dc.remove.tap()
          const delDev = page.getByRole('dialog', { name: 'Удалить девайс?' })
          await sheetFits(page, delDev, 'подтверждение удаления девайса')
          await delDev.getByRole('button', { name: 'Удалить' }).tap()
          await expect(toast(page, 'Отключение выполняется')).toBeVisible()
          await expect(dc.card).toHaveCount(0, { timeout: 45_000 })

          // Заморозка видна в списке после «Назад».
          await panel.getByRole('button', { name: 'Заморозить' }).tap()
          await expect(toast(page, 'Отключение выполняется')).toBeVisible()
          await expect(panel.getByRole('status')).toContainText('Доступ отключён', { timeout: 45_000 })
          await page.getByRole('button', { name: 'Назад' }).tap()
          await expect(panel).toBeHidden()
          await expect(row).toContainText('Доступ отключён')
          await rowFits(row)

          await row.tap()
          await panel.getByRole('button', { name: 'Удалить клиента' }).tap()
          const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
          await sheetFits(page, del, 'подтверждение удаления клиента')
          await del.getByRole('button', { name: 'Удалить' }).tap()
          await expect(toast(page, 'Отключение выполняется')).toBeVisible()
          await expect(panel).toBeHidden({ timeout: 45_000 })
          await expect(page.getByRole('button', { name: 'Назад' })).toHaveCount(0)
          await expect(row).toHaveCount(0)
          const list: { name: string }[] = await (await page.request.get('/api/clients')).json()
          expect(list.map(c => c.name)).not.toContain(name)
        })
      }

      test('длинный ник Telegram не ломает строку списка', async ({ page }) => {
        await openHome(page, '/clients')
        const name = longName()
        expect((await page.request.post('/api/clients', { data: { name } })).status()).toBe(200)
        // Ник ставит только бот при привязке; для вёрстки подменяем его в ответе списка.
        await page.route(u => u.pathname === '/api/clients', async (route) => {
          if (route.request().method() !== 'GET') return route.fallback()
          const res = await route.fetch()
          const body = (await res.json()) as { name: string, tgUsername: string | null, tgLinked: boolean }[]
          for (const c of body) if (c.name === name) Object.assign(c, { tgUsername: 'a_very_long_telegram_username_32', tgLinked: true })
          await route.fulfill({ response: res, json: body })
        })
        await page.reload()
        await page.getByPlaceholder('Поиск...').fill(name)
        const row = clientCard(page, name)
        await expect(row).toContainText('@a_very_long_telegram_username_32')
        await rowFits(row)
        await noHorizontalOverflow(page, 'список с длинным ником')
      })
    })
  }
})
