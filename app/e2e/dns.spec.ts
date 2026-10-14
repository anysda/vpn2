import type { Page } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { openHome } from './support/clients'

// Вкладка DNS против подставного AdGuard Home (e2e/support/fake-backends.mjs):
// в журнале есть ads.example.net из списка и mc.yandex.ru по своему правилу.

const card = (page: Page, title: string) =>
  page.locator('.dns .card').filter({ has: page.getByRole('heading', { name: title, exact: true }) })
const rows = (page: Page) => page.locator('.dns .lt.scroll > div').filter({ has: page.locator('time') })
const grp = (n: number) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')

async function openLog(page: Page) {
  await openHome(page, '/dns')
  await page.getByRole('tab', { name: 'Журнал' }).click()
  await expect(rows(page).first()).toBeVisible()
}

test.describe('DNS', () => {
  test('сводка: цифры AdGuard на карточках', async ({ page }) => {
    await openHome(page, '/dns')
    const s = await (await page.request.get('/api/dns/summary')).json()
    expect(s.queries).toBeGreaterThan(0)
    await expect(card(page, 'Запросы DNS').locator('.big')).toHaveText(grp(s.queries))
    await expect(card(page, 'Заблокировано фильтрами').locator('.big')).toHaveText(grp(s.blocked))
    await expect(card(page, 'Заблокировано угроз').locator('.big')).toHaveText(grp(s.threats))
    await expect(card(page, 'Серверы выше').locator('.up')).toHaveCount(s.upstreams.length)
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
    await expect.poll(async () => [...new Set(await rows(page).locator('.rs .tag').allTextContents())])
      .toEqual(['заблокирован'])

    await page.getByRole('textbox', { name: 'Домен или клиент' }).fill(name)
    await expect.poll(async () => [...new Set(await rows(page).locator(':scope > div:nth-child(4) > b').allTextContents())])
      .toEqual([name])
    await expect(rows(page).filter({ hasText: 'ads.example.net' }).first()).toContainText('AdGuard DNS filter')
  })

  test('журнал: «Разблокировать» снимает блокировку своим правилом', async ({ page }) => {
    await openLog(page)
    await page.getByRole('textbox', { name: 'Домен или клиент' }).fill('mc.yandex.ru')
    const hits = rows(page).filter({ hasText: 'mc.yandex.ru' })
    const tags = async () => [...new Set(await hits.locator('.rs .tag').allTextContents())]
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

    const sw = black.getByRole('switch', { name })
    await sw.click()
    await expect(sw).toHaveAttribute('aria-checked', 'false')
    await expect.poll(async () => (await lists()).find(l => l.name === name)?.enabled).toBe(false)

    await black.locator('.ftab > div').filter({ hasText: name }).getByRole('button', { name: 'Удалить' }).click()
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
    const sw = card(page, 'Чёрные списки').getByRole('switch', { name: 'AdGuard DNS filter' })
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
      await expect(page.locator('.dns .fres')).toContainText('Не фильтруется')

      await area.fill(`${before}\n||example.org^`)
      await page.getByRole('button', { name: 'Применить' }).click()
      await expect(toast(page, 'Свои правила применены')).toBeVisible()
      await expect(page.locator('.dns .fres')).toContainText('Заблокирован')
      await expect(page.locator('.dns .fres code')).toHaveText('||example.org^')
      await expect(page.locator('.dns .fres')).toContainText('из своих правил')

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

    await page.locator('.dns .rwt > div').filter({ hasText: domain }).getByRole('button', { name: 'Удалить' }).click()
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
    const count = page.locator('.dns .svh .tag')
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
    const row = page.locator('.dns .dct.scroll > div').filter({ hasText: name })
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
