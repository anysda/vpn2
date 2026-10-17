import type { Page } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { openHome } from './support/clients'

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

const tab = (page: Page, name: string) => page.locator('.dns .dseg').getByRole('tab', { name })

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
