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
})
