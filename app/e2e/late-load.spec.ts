import type { Page, Route } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { createClientUi, nextYear, openClient, openHome } from './support/clients'

// Поздний ответ сервера не затирает то, что человек уже набрал в форме.
// Ответ держим, пока тест не отпустит: так ввод гарантированно опережает его.
async function hold(page: Page, url: RegExp, method: string) {
  let release!: () => void
  const gate = new Promise<void>((r) => { release = r })
  await page.route(url, async (route: Route) => {
    if (route.request().method() === method) await gate
    await route.fallback()
  })
  return release
}

for (const mobile of [false, true]) {
  test.describe(`свои правила DNS${mobile ? ', телефон' : ''}`, () => {
    if (mobile) test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true })

    test('до загрузки поле не принимает ввод, «Применить» сохраняет видимый текст', async ({ page }) => {
      await openHome(page, '/dns')
      const before = (await (await page.request.get('/api/dns/rules')).json()).text as string
      const mine = `${before}\n||${uniq('late')}.example^`
      const area = page.getByRole('textbox', { name: 'Свои правила' })
      const loaded = await hold(page, /\/api\/dns\/rules$/, 'GET')
      try {
        await page.locator('.dns .dseg').getByRole('tab', { name: 'Свои правила' }).click()
        await expect(area).toBeVisible()
        await expect(area).not.toBeEditable()
        loaded()
        await expect(area).toHaveValue(before)
        await expect(area).toBeEditable()

        await area.fill(mine)
        const saved = await hold(page, /\/api\/dns\/rules$/, 'PUT')
        await page.getByRole('button', { name: 'Применить' }).click()
        await expect(area).not.toBeEditable()
        saved()
        await expect(toast(page, 'Свои правила применены')).toBeVisible()
        await expect(area).toHaveValue(mine)
        await expect(area).toBeEditable()
        expect((await (await page.request.get('/api/dns/rules')).json()).text).toBe(mine)
      }
      finally {
        const r = await page.request.put('/api/dns/rules', { data: { text: before } })
        expect(r.status(), await r.text()).toBe(200)
      }
    })
  })
}

test('настройки DNS: пока серверы сохраняются, поле не принимает ввод', async ({ page }) => {
  await openHome(page, '/dns')
  await page.getByRole('tab', { name: 'Настройки DNS' }).click()
  const view = async () => (await (await page.request.get('/api/dns/settings')).json()) as { upstreams: string }
  const before = (await view()).upstreams
  const ups = page.getByRole('textbox', { name: 'Серверы выше' })
  await expect(ups).toHaveValue(before)
  try {
    const mine = `${before}\n192.0.2.77`
    const saved = await hold(page, /\/api\/dns\/settings$/, 'PATCH')
    await ups.fill(mine)
    await ups.blur()
    await expect(ups).not.toBeEditable()
    saved()
    await expect(toast(page, 'Серверы выше: сохранено')).toBeVisible()
    await expect(ups).toHaveValue(mine)
    await expect(ups).toBeEditable()
    expect((await view()).upstreams).toBe(mine)
  }
  finally {
    const r = await page.request.patch('/api/dns/settings', { data: { upstreams: before } })
    expect(r.status(), await r.text()).toBe(200)
  }
})

test('клиент: перезагрузка карточки после срока не затирает набранный лимит', async ({ page }) => {
  await openHome(page, '/clients')
  const name = uniq('late-limit')
  await createClientUi(page, name)
  const panel = await openClient(page, name)
  const limit = panel.getByRole('textbox', { name: 'Лимит девайсов' })
  // Пока карточка перезагружается, лимит в другой вкладке сменили на 5.
  let reload!: () => void
  const gate = new Promise<void>((r) => { reload = r })
  await page.route(/\/api\/clients\/\d+$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await gate
    const res = await route.fetch()
    await route.fulfill({ response: res, json: { ...(await res.json()), deviceLimit: 5 } })
  })
  await panel.getByLabel('Истекает').fill(nextYear().replace(/\D/g, ''))
  await limit.click()
  await expect(toast(page, 'Срок действия сохранён')).toBeVisible()
  await limit.fill('7')
  reload()
  await expect(panel.getByRole('heading', { name: 'Девайсы 0 / 5' })).toBeVisible()
  await expect(panel.getByLabel('Истекает')).toHaveValue(nextYear())
  await expect(limit).toHaveValue('7')
  await limit.press('Enter')
  await expect(toast(page, 'Лимит девайсов сохранён')).toBeVisible()
  const list: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
  const id = list.find(c => c.name === name)!.id
  expect((await (await page.request.get(`/api/clients/${id}`)).json()).deviceLimit).toBe(7)
})

test('проверка домена, набранного сразу после «Применить», дожидается новых правил', async ({ page }) => {
  await openHome(page, '/dns')
  const before = (await (await page.request.get('/api/dns/rules')).json()).text as string
  const host = `${uniq('fresh')}.example`
  const area = page.getByRole('textbox', { name: 'Свои правила' })
  try {
    await page.locator('.dns .dseg').getByRole('tab', { name: 'Свои правила' }).click()
    await area.fill(`${before}\n||${host}^`)
    await page.getByRole('button', { name: 'Применить' }).click()
    await expect(toast(page, 'Свои правила применены')).toBeVisible()
    // Первые ответы после применения — ещё по старым правилам, как у AdGuard.
    let stale = 2
    await page.route(/\/api\/dns\/check\?/, async (route) => {
      if (stale-- > 0) return route.fulfill({ json: { kind: 'ok', label: 'Не фильтруется', why: ['', '', ''] } })
      await route.fallback()
    })
    await page.getByRole('textbox', { name: 'Домен для проверки' }).fill(host)
    await expect(page.locator('.dns .fres')).toContainText('Заблокирован')
    expect(stale).toBeLessThan(0)
  }
  finally {
    const r = await page.request.put('/api/dns/rules', { data: { text: before } })
    expect(r.status(), await r.text()).toBe(200)
  }
})
