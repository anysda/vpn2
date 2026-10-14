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
})
