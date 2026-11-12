import type { Page } from '@playwright/test'
import { expect, toast, uniq, test } from './support/fixtures'
import { openHome } from './support/clients'
import { CLOCK_MARGIN_MS, pauseClockAt } from './support/clock'
import { noHorizontalOverflow, noVerticalOverflow } from './support/overflow'

test.describe('dns', () => {
  // Вкладка DNS против подставного AdGuard Home (e2e/support/fake-backends.mjs):
  // в журнале есть ads.example.net из списка и mc.yandex.ru по своему правилу.

  const card = (page: Page, title: string) =>
    page.locator('.dns .card').filter({ has: page.getByRole('heading', { name: title, exact: true }) })
  const rows = (page: Page) => page.getByTestId('dns-log-row').filter({ has: page.locator('time') })
  const grp = (n: number) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')

  async function openLog(page: Page) {
    await openHome(page, '/dns')
    await page.getByRole('tab', { name: 'Журнал' }).click()
    await expect(rows(page).first()).toBeVisible()
  }

  test.describe('DNS', () => {
    // Окно ставит курсор в первое поле с задержкой. Если человек уже перешёл в
    // другое поле, задержка не должна уводить курсор и текст в первое.
    test('окно: ввод во второе поле до автофокуса остаётся во втором', async ({ page }) => {
      await openHome(page, '/dns')
      await page.getByRole('tab', { name: 'Перезаписи' }).click()
      await expect(page.getByRole('button', { name: 'Добавить перезапись' })).toBeVisible()
      await pauseClockAt(page, Date.now() + CLOCK_MARGIN_MS)
      await page.getByRole('button', { name: 'Добавить перезапись' }).click()
      const dlg = page.getByRole('dialog', { name: 'Добавить перезапись' })
      const answer = dlg.getByRole('textbox', { name: 'Ответ' })
      await answer.fill('192.0.2.10')
      await page.clock.runFor(500)
      await expect(answer).toBeFocused()
      await expect(answer).toHaveValue('192.0.2.10')
      await expect(dlg.getByRole('textbox', { name: 'Домен' })).toHaveValue('')
      // Без своего выбора курсор, как и раньше, — в первом поле.
      await dlg.getByRole('button', { name: 'Отмена' }).click()
      await page.getByRole('button', { name: 'Добавить перезапись' }).click()
      await page.clock.runFor(500)
      await expect(dlg.getByRole('textbox', { name: 'Домен' })).toBeFocused()
    })

    test('сводка: цифры AdGuard на карточках', async ({ page }) => {
      await openHome(page, '/dns')
      const s = await (await page.request.get('/api/dns/summary')).json()
      expect(s.queries).toBeGreaterThan(0)
      await expect(card(page, 'Запросы DNS').locator('.big')).toHaveText(grp(s.queries))
      await expect(card(page, 'Заблокировано фильтрами').locator('.big')).toHaveText(grp(s.blocked))
      await expect(card(page, 'Заблокировано угроз').locator('.big')).toHaveText(grp(s.threats))
      await expect(card(page, 'Серверы выше').getByTestId('dns-upstream')).toHaveCount(s.upstreams.length)
      await expect(card(page, 'Частые блокировки')).toContainText('mc.yandex.ru')
    })

    test('журнал: фильтр «Заблокированные» и поиск по имени клиента панели', async ({ page }) => {
      await openHome(page)
      const name = uniq('dns')
      const c = await page.request.post('/api/clients', { data: { name } })
      expect(c.status(), await c.text()).toBe(200)
      const d = await page.request.post(`/api/clients/${(await c.json()).id}/devices`, { data: { name: 'ноутбук' } })
      expect(d.status(), await d.text()).toBe(200)

      await openLog(page)
      await page.getByRole('tab', { name: 'Заблокированные' }).click()
      await expect.poll(async () => [...new Set(await rows(page).locator('.tag').allTextContents())])
        .toEqual(['заблокирован'])

      await page.getByRole('textbox', { name: 'Домен или клиент' }).fill(name)
      await expect.poll(async () => [...new Set(await rows(page).locator(':scope > div:nth-child(4) > span').allTextContents())])
        .toEqual([name])
      await expect(rows(page).filter({ hasText: 'ads.example.net' }).first()).toContainText('AdGuard DNS filter')
    })

    test('журнал: «Разблокировать» снимает блокировку своим правилом', async ({ page }) => {
      await openLog(page)
      await page.getByRole('textbox', { name: 'Домен или клиент' }).fill('mc.yandex.ru')
      const hits = rows(page).filter({ hasText: 'mc.yandex.ru' })
      const tags = async () => [...new Set(await hits.locator('.tag').allTextContents())]
      await expect.poll(tags).toEqual(['заблокирован'])
      await hits.first().getByRole('button', { name: 'Разблокировать' }).click()
      await expect(toast(page, 'mc.yandex.ru: разблокирован своим правилом')).toBeVisible()
      await expect.poll(tags).toEqual(['разрешён'])
      await expect(hits.first()).toContainText('Свои правила')
    })

    test('фильтры: список добавляется, выключается и удаляется в AdGuard', async ({ page, guard }) => {
      guard.allow(400, /\/api\/dns\/filters$/, 'POST')
      await openHome(page, '/dns')
      await page.getByRole('tab', { name: 'Фильтры' }).click()
      const black = card(page, 'Чёрные списки')
      const lists = async () => (await (await page.request.get('/api/dns/filters')).json()).black as Array<{ name: string, enabled: boolean }>
      const name = uniq('list')

      await black.getByRole('button', { name: 'Добавить список' }).click()
      const dlg = page.getByRole('dialog', { name: 'Добавить список' })
      await dlg.getByRole('textbox', { name: 'Название' }).fill(name)
      await dlg.getByRole('textbox', { name: 'Адрес списка' }).fill('https://lists.invalid/x.txt')
      await dlg.getByRole('button', { name: 'Добавить', exact: true }).click()
      await expect(toast(page, 'Couldn\'t fetch filter')).toBeVisible()
      await expect(dlg.getByRole('textbox', { name: 'Название' })).toHaveValue(name)

      await dlg.getByRole('textbox', { name: 'Адрес списка' }).fill(`https://lists.example.com/${name}.txt`)
      await dlg.getByRole('button', { name: 'Добавить', exact: true }).click()
      await expect(toast(page, `Список «${name}» добавлен`)).toBeVisible()
      await expect.poll(async () => (await lists()).find(l => l.name === name)?.enabled).toBe(true)

      const row = black.getByTestId('dns-list-row').filter({ hasText: name })
      const sw = row.getByRole('switch')
      await sw.click()
      await expect(sw).toHaveAttribute('aria-checked', 'false')
      await expect.poll(async () => (await lists()).find(l => l.name === name)?.enabled).toBe(false)

      await row.getByRole('button', { name: 'Удалить' }).click()
      await page.getByRole('dialog', { name: 'Удалить список?' }).getByRole('button', { name: 'Удалить', exact: true }).click()
      await expect(toast(page, `Список «${name}» удалён`)).toBeVisible()
      await expect(sw).toHaveCount(0)
      expect((await lists()).some(l => l.name === name)).toBe(false)
    })

    test('фильтры: выключенный список перестаёт блокировать', async ({ page }) => {
      await openHome(page, '/dns')
      const verdict = async () => (await (await page.request.get('/api/dns/check?name=ads.example.net')).json()).label
      await expect.poll(verdict).toBe('Заблокирован')
      await page.getByRole('tab', { name: 'Фильтры' }).click()
      const sw = card(page, 'Чёрные списки').getByTestId('dns-list-row').filter({ hasText: 'AdGuard DNS filter' }).getByRole('switch')
      await sw.click()
      try {
        await expect(sw).toHaveAttribute('aria-checked', 'false')
        await expect.poll(verdict).toBe('Не фильтруется')
      }
      finally {
        await sw.click()
        await expect(sw).toHaveAttribute('aria-checked', 'true')
      }
      await expect.poll(verdict).toBe('Заблокирован')
    })

    test('свои правила: правило блокирует, проверка его называет; строка с ошибкой не теряется', async ({ page, guard }) => {
      guard.allow(422, /\/api\/dns\/rules$/, 'PUT')
      await openHome(page, '/dns')
      const before = (await (await page.request.get('/api/dns/rules')).json()).text as string
      await page.getByRole('tab', { name: 'Свои правила' }).click()
      const area = page.getByRole('textbox', { name: 'Свои правила' })
      await expect(area).toHaveValue(before)
      try {
        await page.getByRole('textbox', { name: 'Домен для проверки' }).fill('example.org')
        await expect(page.getByTestId('dns-check')).toContainText('Не фильтруется')

        await area.fill(`${before}\n||example.org^`)
        await page.getByRole('button', { name: 'Применить' }).click()
        await expect(toast(page, 'Свои правила применены')).toBeVisible()
        await expect(page.getByTestId('dns-check')).toContainText('Заблокирован')
        await expect(page.getByTestId('dns-check').locator('code')).toHaveText('||example.org^')
        await expect(page.getByTestId('dns-check')).toContainText('из своих правил')

        const bad = `${before}\n||example.org^\n||foo.example^$bogus`
        await area.fill(bad)
        await page.getByRole('button', { name: 'Применить' }).click()
        await expect(toast(page, `Строка ${bad.split('\n').length}: неизвестный модификатор $bogus`)).toBeVisible()
        await expect(area).toHaveValue(bad)
        expect((await (await page.request.get('/api/dns/rules')).json()).text).toBe(`${before}\n||example.org^`)
      }
      finally {
        const r = await page.request.put('/api/dns/rules', { data: { text: before } })
        expect(r.status(), await r.text()).toBe(200)
      }
    })

    test('перезаписи: добавляются, выключаются и удаляются в AdGuard; дубль — отказ', async ({ page, guard }) => {
      guard.allow(409, /\/api\/dns\/rewrites$/, 'POST')
      await openHome(page, '/dns')
      await page.getByRole('tab', { name: 'Перезаписи' }).click()
      const domain = `${uniq('rw')}.lan`
      const list = async () => (await (await page.request.get('/api/dns/rewrites')).json()) as Array<{ domain: string, answer: string, enabled: boolean }>
      const add = async (d: string) => {
        await page.getByRole('button', { name: 'Добавить перезапись' }).click()
        const dlg = page.getByRole('dialog', { name: 'Добавить перезапись' })
        await dlg.getByRole('textbox', { name: 'Домен' }).fill(d)
        await dlg.getByRole('textbox', { name: 'Ответ' }).fill('192.0.2.10')
        await dlg.getByRole('button', { name: 'Добавить', exact: true }).click()
        return dlg
      }
      await add(`${domain.toUpperCase()}.`)
      await expect(toast(page, `Перезапись «${domain}» добавлена`)).toBeVisible()
      expect((await list()).filter(r => r.domain === domain)).toEqual([{ domain, answer: '192.0.2.10', enabled: true }])

      const dlg = await add(domain)
      await expect(toast(page, `Перезапись «${domain}» → 192.0.2.10 уже есть`)).toBeVisible()
      await expect(dlg).toBeVisible()
      await dlg.getByRole('button', { name: 'Отмена' }).click()
      expect((await list()).filter(r => r.domain === domain)).toHaveLength(1)

      const sw = page.getByRole('switch', { name: domain })
      await sw.click()
      await expect(sw).toHaveAttribute('aria-checked', 'false')
      await expect.poll(async () => (await list()).find(r => r.domain === domain)?.enabled).toBe(false)

      await page.getByTestId('dns-list-row').filter({ hasText: domain }).getByRole('button', { name: 'Удалить' }).click()
      await page.getByRole('dialog', { name: 'Удалить перезапись?' }).getByRole('button', { name: 'Удалить', exact: true }).click()
      await expect(toast(page, `Перезапись «${domain}» удалена`)).toBeVisible()
      await expect(sw).toHaveCount(0)
      expect((await list()).some(r => r.domain === domain)).toBe(false)
    })

    test('сервисы: карточка блокирует сервис в AdGuard и снимает блокировку', async ({ page }) => {
      await openHome(page, '/dns')
      await page.getByRole('tab', { name: 'Сервисы' }).click()
      const blocked = async () => (await (await page.request.get('/api/dns/services')).json()).blocked as string[]
      const before = await blocked()
      const sv = page.getByRole('switch', { name: 'Instagram' })
      await expect(sv).toHaveAttribute('aria-checked', String(before.includes('instagram')))
      const count = page.getByTestId('dns-services-count')
      await expect(count).toHaveText(`Заблокировано ${before.length} из 3`)
      try {
        await sv.click()
        await expect(sv).toHaveAttribute('aria-checked', String(!before.includes('instagram')))
        await expect.poll(async () => (await blocked()).includes('instagram')).toBe(!before.includes('instagram'))
        await expect(count).not.toHaveText(`Заблокировано ${before.length} из 3`)
        await expect.poll(async () => (await (await page.request.get('/api/dns/check?name=instagram.com')).json()).label)
          .toBe(before.includes('instagram') ? 'Не фильтруется' : 'Заблокирован')
      }
      finally {
        const r = await page.request.put('/api/dns/services', { data: { ids: before } })
        expect(r.status(), await r.text()).toBe(200)
      }
    })

    test('клиенты: клиент панели виден по адресу в туннеле', async ({ page }) => {
      await openHome(page)
      const name = uniq('dnscl')
      const c = await page.request.post('/api/clients', { data: { name } })
      expect(c.status(), await c.text()).toBe(200)
      const d = await page.request.post(`/api/clients/${(await c.json()).id}/devices`, { data: { name: 'телефон' } })
      expect(d.status(), await d.text()).toBe(200)
      const rows = (await (await page.request.get('/api/dns/clients')).json()) as Array<{ name: string, ips: string[] }>
      const ips = rows.find(r => r.name === name)?.ips ?? []
      expect(ips.length).toBeGreaterThan(0)
      await page.goto('/dns')
      await page.getByRole('tab', { name: 'Клиенты' }).click()
      const row = page.getByTestId('dns-client-row').filter({ hasText: name })
      await expect(row).toContainText('без Telegram')
      await expect(row).toContainText(ips[0]!)
      await expect(row).toContainText('общие')
      await expect(row).toContainText('как у всех')
    })

    test('настройки DNS: переключатель, поле «Изменить», отказ AdGuard и проверка серверов', async ({ page, guard }) => {
      guard.allow(422, /\/api\/dns\/settings$/, 'PATCH')
      await openHome(page, '/dns')
      await page.getByRole('tab', { name: 'Настройки DNS' }).click()
      const view = async () => (await (await page.request.get('/api/dns/settings')).json()) as Record<string, unknown>
      const before = await view()
      try {
        const sw = page.getByRole('switch', { name: 'DNSSEC' })
        await sw.click()
        await expect(sw).toHaveAttribute('aria-checked', String(!before.dnssec))
        await expect.poll(async () => (await view()).dnssec).toBe(!before.dnssec)

        const limit = page.locator('.dns .fld').filter({ hasText: 'Лимит запросов' })
        await limit.getByRole('button', { name: 'Изменить' }).click()
        const dlg = page.getByRole('dialog', { name: 'Лимит запросов' })
        await dlg.getByRole('textbox', { name: 'Лимит запросов' }).fill('много')
        await dlg.getByRole('button', { name: 'Сохранить' }).click()
        await expect(toast(page, /^Лимит запросов:.*числ/)).toBeVisible()
        await expect(dlg).toBeVisible()
        await dlg.getByRole('textbox', { name: 'Лимит запросов' }).fill('35')
        await dlg.getByRole('button', { name: 'Сохранить' }).click()
        await expect(toast(page, 'Лимит запросов: сохранено')).toBeVisible()
        await expect(limit.locator('code')).toHaveText('35 в секунду с клиента')

        const ups = page.getByRole('textbox', { name: 'Серверы выше' })
        await expect(ups).toHaveValue(String(before.upstreams))
        await page.getByRole('button', { name: 'Проверить' }).click()
        await expect(toast(page, 'Все 2 сервера отвечают')).toBeVisible()
        const bad = `${before.upstreams}\nbad host`
        await ups.fill(bad)
        await ups.blur()
        await expect(toast(page, 'Строка 3: AdGuard не понял адрес сервера')).toBeVisible()
        await expect(ups).toHaveValue(bad)
        expect((await view()).upstreams).toBe(before.upstreams)
        await ups.fill(`${before.upstreams}\nhttps://dns.invalid/dns-query`)
        await page.getByRole('button', { name: 'Проверить' }).click()
        await expect(toast(page, 'Не отвечают: https://dns.invalid/dns-query')).toBeVisible()
      }
      finally {
        for (const body of [{ dnssec: before.dnssec }, { ratelimit: before.ratelimit }, { upstreams: before.upstreams }]) {
          const r = await page.request.patch('/api/dns/settings', { data: body })
          expect(r.status(), await r.text()).toBe(200)
        }
      }
    })

  })
})

test.describe('dns-load-retry', () => {
  // Подраздел DNS, чей первый GET упал, показывает ошибку и «Повторить загрузку»;
  // по кнопке подраздел загружается без перезагрузки страницы.
  const tab = (page: Page, name: string) => page.locator('.dns .head .seg').getByRole('tab', { name })
  const alert = (page: Page) => page.locator('.dns [data-testid="dns-panel"]:visible').getByRole('alert').filter({ hasText: 'AdGuard не ответил (e2e)' })

  // $fetch сам повторяет упавший GET один раз, поэтому GET падает, пока тест не
  // «починит» AdGuard перед нажатием «Повторить загрузку».
  async function failGet(page: Page, url: RegExp) {
    await page.route(url, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      await route.fulfill({ status: 500, json: { message: 'AdGuard не ответил (e2e)' } })
    })
  }

  async function retry(page: Page, url: RegExp) {
    await page.unrouteAll({ behavior: 'wait' })
    const ok = page.waitForResponse(r => url.test(r.url()) && r.request().method() === 'GET' && r.status() === 200)
    await page.locator('.dns [data-testid="dns-panel"]:visible').getByRole('button', { name: 'Повторить загрузку' }).click()
    await ok
    await expect(alert(page)).toHaveCount(0)
  }

  test('свои правила: после ошибки загрузки «Повторить» открывает поле с текстом сервера', async ({ page, guard }) => {
    const url = /\/api\/dns\/rules$/
    guard.allow(500, url, 'GET')
    await openHome(page, '/dns')
    const text = (await (await page.request.get('/api/dns/rules')).json()).text as string
    await failGet(page, url)
    await tab(page, 'Свои правила').click()
    const area = page.getByRole('textbox', { name: 'Свои правила' })
    await expect(alert(page)).toBeVisible()
    await expect(area).not.toBeEditable()
    await expect(page.getByRole('button', { name: 'Применить' })).toBeDisabled()
    await retry(page, url)
    await expect(area).toHaveValue(text)
    await expect(area).toBeEditable()
    await expect(page.getByRole('button', { name: 'Применить' })).toBeEnabled()
  })

  test('настройки DNS: после ошибки загрузки «Повторить» показывает настройки', async ({ page, guard }) => {
    const url = /\/api\/dns\/settings$/
    guard.allow(500, url, 'GET')
    await openHome(page, '/dns')
    const ups = (await (await page.request.get('/api/dns/settings')).json()).upstreams as string
    await failGet(page, url)
    await tab(page, 'Настройки DNS').click()
    await expect(alert(page)).toBeVisible()
    await retry(page, url)
    await expect(page.getByRole('textbox', { name: 'Серверы выше' })).toHaveValue(ups)
    await expect(page.getByRole('switch', { name: 'Оптимистичный кэш' })).toBeEnabled()
  })

  test('фильтры: после ошибки загрузки «Повторить» показывает списки', async ({ page, guard }) => {
    const url = /\/api\/dns\/filters$/
    guard.allow(500, url, 'GET')
    await openHome(page, '/dns')
    await failGet(page, url)
    await tab(page, 'Фильтры').click()
    await expect(alert(page)).toBeVisible()
    await retry(page, url)
    await expect(page.getByTestId('dns-list-row').filter({ hasText: 'AdGuard DNS filter' }).getByRole('switch')).toBeVisible()
  })

  test('перезаписи: после ошибки загрузки «Повторить» показывает список', async ({ page, guard }) => {
    const url = /\/api\/dns\/rewrites$/
    guard.allow(500, url, 'GET')
    await openHome(page, '/dns')
    await failGet(page, url)
    await tab(page, 'Перезаписи').click()
    await expect(alert(page)).toBeVisible()
    await retry(page, url)
  })

  test('сервисы: после ошибки загрузки «Повторить» показывает каталог', async ({ page, guard }) => {
    const url = /\/api\/dns\/services$/
    guard.allow(500, url, 'GET')
    await openHome(page, '/dns')
    const all = (await (await page.request.get('/api/dns/services')).json()).all as unknown[]
    await failGet(page, url)
    await tab(page, 'Сервисы').click()
    await expect(alert(page)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Разблокировать все' })).toBeDisabled()
    await retry(page, url)
    await expect(page.getByTestId('dns-services').getByRole('switch')).toHaveCount(all.length)
    await expect(page.getByRole('button', { name: 'Разблокировать все' })).toBeEnabled()
  })

  test('клиенты DNS: после ошибки загрузки «Повторить» показывает клиентов', async ({ page, guard }) => {
    const url = /\/api\/dns\/clients$/
    guard.allow(500, url, 'GET')
    await openHome(page, '/dns')
    await failGet(page, url)
    await tab(page, 'Клиенты').click()
    await expect(alert(page)).toBeVisible()
    await retry(page, url)
  })
})

test.describe('dns-race', () => {
  // Два изменения подряд: ответ первого посчитан сервером до второго, а дошёл
  // последним. Форма не должна вернуться к состоянию из этого ответа.
  async function late(page: Page, url: RegExp, method: string) {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    let fetched!: () => void
    const done = new Promise<void>((r) => { fetched = r })
    let first = true
    await page.route(url, async (route) => {
      if (route.request().method() !== method || !first) return route.fallback()
      first = false
      const res = await route.fetch()
      fetched()
      await gate
      await route.fulfill({ response: res })
    })
    // Ответ дошёл до страницы и она успела его применить.
    const delivered = async () => {
      const req = page.waitForEvent('requestfinished', r => url.test(r.url()) && r.method() === method)
      release()
      await req
      await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
    }
    return { fetched: done, delivered }
  }

  const tab = (page: Page, name: string) => page.locator('.dns .head .seg').getByRole('tab', { name })

  test('настройки DNS: поздний ответ переключателя не возвращает в «Серверы выше» прежний список', async ({ page }) => {
    const view = async () => (await (await page.request.get('/api/dns/settings')).json()) as { upstreams: string, optimistic: boolean }
    await openHome(page, '/dns')
    const before = await view()
    await tab(page, 'Настройки DNS').click()
    const ups = page.getByRole('textbox', { name: 'Серверы выше' })
    const sw = page.getByRole('switch', { name: 'Оптимистичный кэш' })
    await expect(ups).toHaveValue(before.upstreams)
    try {
      const stale = await late(page, /\/api\/dns\/settings$/, 'PATCH')
      await sw.click()
      await stale.fetched
      const mine = `${before.upstreams}\n192.0.2.78`
      await ups.fill(mine)
      await ups.blur()
      await expect(toast(page, 'Серверы выше: сохранено')).toBeVisible()
      await expect(ups).toHaveValue(mine)
      await stale.delivered()
      await expect(ups).toHaveValue(mine)
      const now = await view()
      expect(now.upstreams).toBe(mine)
      await expect(ups).toHaveValue(now.upstreams)
      await expect(sw).toHaveAttribute('aria-checked', String(now.optimistic))
      await expect(sw).toHaveAttribute('aria-checked', String(!before.optimistic))
    }
    finally {
      for (const data of [{ upstreams: before.upstreams }, { optimistic: before.optimistic }]) {
        const r = await page.request.patch('/api/dns/settings', { data })
        expect(r.status(), await r.text()).toBe(200)
      }
    }
  })

  test('фильтры: поздний ответ первого переключателя не включает обратно второй список', async ({ page }) => {
    const names = [uniq('race-a'), uniq('race-b')]
    const url = (n: string) => `https://lists.example.com/${n}.txt`
    await openHome(page, '/dns')
    for (const name of names) {
      const r = await page.request.post('/api/dns/filters', { data: { name, url: url(name), white: false } })
      expect(r.status(), await r.text()).toBe(200)
    }
    const enabled = async (n: string) => ((await (await page.request.get('/api/dns/filters')).json()).black as { url: string, enabled: boolean }[]).find(l => l.url === url(n))?.enabled
    try {
      await tab(page, 'Фильтры').click()
      const [a, b] = names.map(n => page.getByRole('switch', { name: n }))
      await expect(b!).toHaveAttribute('aria-checked', 'true')
      const stale = await late(page, /\/api\/dns\/filters$/, 'PATCH')
      await a!.click()
      await stale.fetched
      await b!.click()
      await expect.poll(() => enabled(names[1]!)).toBe(false)
      await expect(b!).toHaveAttribute('aria-checked', 'false')
      await stale.delivered()
      await expect(b!).toHaveAttribute('aria-checked', 'false')
      await expect(a!).toHaveAttribute('aria-checked', 'false')
    }
    finally {
      for (const name of names) await page.request.delete('/api/dns/filters', { data: { url: url(name), white: false } })
    }
  })

  test('перезаписи: поздний ответ первого переключателя не включает обратно вторую перезапись', async ({ page }) => {
    const domains = [`${uniq('race-a')}.lan`, `${uniq('race-b')}.lan`]
    await openHome(page, '/dns')
    for (const domain of domains) {
      const r = await page.request.post('/api/dns/rewrites', { data: { domain, answer: '192.0.2.11' } })
      expect(r.status(), await r.text()).toBe(200)
    }
    const enabled = async (d: string) => ((await (await page.request.get('/api/dns/rewrites')).json()) as { domain: string, enabled: boolean }[]).find(r => r.domain === d)?.enabled
    try {
      await tab(page, 'Перезаписи').click()
      const [a, b] = domains.map(d => page.getByRole('switch', { name: d }))
      await expect(b!).toHaveAttribute('aria-checked', 'true')
      const stale = await late(page, /\/api\/dns\/rewrites$/, 'PATCH')
      await a!.click()
      await stale.fetched
      await b!.click()
      await expect.poll(() => enabled(domains[1]!)).toBe(false)
      await expect(b!).toHaveAttribute('aria-checked', 'false')
      await stale.delivered()
      await expect(b!).toHaveAttribute('aria-checked', 'false')
      await expect(a!).toHaveAttribute('aria-checked', 'false')
    }
    finally {
      for (const domain of domains) await page.request.delete('/api/dns/rewrites', { data: { domain, answer: '192.0.2.11' } })
    }
  })
})

test.describe('mobile-dns', { tag: '@mobile' }, () => {
  // DNS на телефоне: восемь подразделов в одну колонку, окна поверх страницы.
  // Длинные имена клиентов и адреса не должны вылезать за край экрана.

  const SUBS = ['Сводка', 'Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы', 'Клиенты', 'Настройки DNS']

  const CLIENTS = [
    { name: 'Дмитрий Козлов', tg: '@dkozlov', ips: ['10.66.66.2', 'fd42:42:42::2', '10.66.66.12'], filters: 'common', safesearch: 'global', services: null, queries: 12480 },
    { name: 'Анна Петрова-Водкина-Длинная-Фамилия', tg: '@anna_petrova_with_a_very_long_handle', ips: ['10.66.66.3'], filters: 'off', safesearch: 'on', services: 'TikTok, YouTube, Instagram', queries: 914 },
  ]

  // Видимые элементы подраздела, которые торчат за край окна или срезаны краем
  // своей карточки. Ряды со своей горизонтальной прокруткой не в счёт.
  function stickingOut(page: Page) {
    return page.evaluate(() => {
      const w = document.documentElement.clientWidth
      const cut = (el: Element, r: DOMRect) => {
        for (let p = el.parentElement; p; p = p.parentElement) {
          const o = getComputedStyle(p).overflowX
          if (o === 'auto' || o === 'scroll') return false
          if (o === 'hidden' || o === 'clip') {
            const b = p.getBoundingClientRect()
            if (r.right > b.right + 1 || r.left < b.left - 1) return true
          }
        }
        return r.right > w + 0.5 || r.left < -0.5
      }
      return [...document.querySelectorAll('.dns [data-testid="dns-panel"]:not([style*="none"]) *')]
        .filter((el) => {
          const r = el.getBoundingClientRect()
          return r.width > 0 && r.height > 0 && cut(el, r)
        })
        .slice(0, 5)
        .map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} «${(el.textContent ?? '').trim().slice(0, 40)}»`)
    })
  }

  const sheet = (page: Page, name: string) => page.getByRole('dialog', { name })

  for (const width of [360, 402]) {
    test.describe(`DNS на телефоне ${width}`, () => {
      test.use({ viewport: { width, height: 874 } })

      test('сводка: заголовок, числа и график внутри карточки', async ({ page }) => {
        await openHome(page, '/dns')
        await expect(page.getByTestId('dns-kpi').locator(':scope > .card')).toHaveCount(4)
        await noVerticalOverflow(page, '[data-testid="dns-kpi"]', `сводка ${width}`)
      })

      for (const scheme of ['dark', 'light'] as const) {
        test(`восемь подразделов в ширину экрана, ${scheme}`, async ({ page }) => {
          await page.emulateMedia({ colorScheme: scheme })
          await page.route('**/api/dns/clients', route => route.fulfill({ json: CLIENTS }))
          await openHome(page, '/dns')
          const seg = page.locator('.dns .head .seg')
          expect(await seg.evaluate(e => e.scrollWidth > e.clientWidth)).toBe(true)

          for (const name of SUBS) {
            const tab = seg.getByRole('tab', { name, exact: true })
            await tab.tap()
            await expect(tab).toHaveAttribute('aria-selected', 'true')
            await expect(tab).toBeInViewport({ ratio: 1 })
            await expect(page.locator('.dns [data-testid="dns-panel"]:visible')).toBeVisible()
            await page.waitForLoadState('networkidle')
            // Снимок всей страницы перекладывает её по полной высоте: так видно
            // раскладку, которая держится только на первом проходе.
            await page.screenshot({ fullPage: true })
            await noHorizontalOverflow(page, name)
            expect(await stickingOut(page), name).toEqual([])
          }
        })
      }
    })
  }

  test.describe('DNS на телефоне: раскладка и окна', () => {
    test('журнал строкой с деталями: время и клиент под доменом', async ({ page }) => {
      await openHome(page, '/dns')
      await page.locator('.dns .head .seg').getByRole('tab', { name: 'Журнал' }).tap()
      const row = page.getByTestId('dns-log-row').filter({ has: page.locator('time') }).first()
      await expect(row.locator('small > span')).toBeVisible()
      await expect(row.locator('time')).toBeHidden()
    })

    test('правило и проверка, перезапись, список и сброс — в окнах', async ({ page, guard }) => {
      guard.allow(409, /\/api\/dns\/rewrites$/, 'POST')
      await openHome(page, '/dns')
      const seg = page.locator('.dns .head .seg')
      const before = (await (await page.request.get('/api/dns/rules')).json()).text as string
      try {
        await seg.getByRole('tab', { name: 'Свои правила' }).tap()
        await page.getByRole('textbox', { name: 'Свои правила' }).fill(`${before}\n||example.org^`)
        await page.getByRole('button', { name: 'Применить' }).tap()
        await expect(toast(page, 'Свои правила применены')).toBeVisible()
        await page.getByRole('textbox', { name: 'Домен для проверки' }).fill('example.org')
        await expect(page.getByTestId('dns-check')).toContainText('Заблокирован')
        await expect(page.getByTestId('dns-check').locator('code')).toHaveText('||example.org^')
      }
      finally {
        const r = await page.request.put('/api/dns/rules', { data: { text: before } })
        expect(r.status(), await r.text()).toBe(200)
      }

      await seg.getByRole('tab', { name: 'Перезаписи' }).tap()
      const domain = `${uniq('rw')}.lan`
      await page.getByRole('button', { name: 'Добавить перезапись' }).tap()
      const add = sheet(page, 'Добавить перезапись')
      await expect(add).toBeVisible()
      await add.getByRole('textbox', { name: 'Домен' }).fill(domain)
      await add.getByRole('textbox', { name: 'Ответ' }).fill('10.0.0.1')
      await add.getByRole('button', { name: 'Добавить', exact: true }).tap()
      await expect(toast(page, `Перезапись «${domain}» добавлена`)).toBeVisible()
      await expect(add).toBeHidden()
      const rw = page.getByTestId('dns-list-row').filter({ hasText: domain })
      await expect(rw).toContainText('10.0.0.1')
      await rw.getByRole('button', { name: 'Удалить' }).tap()
      const del = sheet(page, 'Удалить перезапись?')
      await expect(del).toBeVisible()
      await del.getByRole('button', { name: 'Удалить', exact: true }).tap()
      await expect(toast(page, `Перезапись «${domain}» удалена`)).toBeVisible()
      await expect(rw).toHaveCount(0)

      await seg.getByRole('tab', { name: 'Фильтры' }).tap()
      await page.getByRole('button', { name: 'Добавить список' }).first().tap()
      const list = sheet(page, 'Добавить список')
      await expect(list.getByRole('textbox', { name: 'Адрес списка' })).toBeVisible()
      await page.touchscreen.tap(20, 20)
      await expect(list).toBeHidden()

      await seg.getByRole('tab', { name: 'Настройки DNS' }).tap()
      await page.getByRole('button', { name: 'Журнал', exact: true }).tap()
      const ask = sheet(page, 'Сбросить журнал?')
      await expect(ask).toBeVisible()
      await ask.getByRole('button', { name: 'Отмена' }).tap()
      await expect(ask).toBeHidden()
    })
  })
})
