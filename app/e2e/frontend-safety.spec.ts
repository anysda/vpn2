import { ADMIN_USER, expect, hydrated, test } from './support/fixtures'
import { addDevice, createClientUi, deviceCard, openClient, openHome } from './support/clients'

for (const width of [1400, 390]) {
  test.describe(`безопасность интерфейса, ${width}px`, () => {
    test.use({ viewport: { width, height: 844 } })

    test('HTML в именах клиента и девайса остаётся текстом в карточках и модалках', async ({ page }) => {
      await openHome(page, '/clients')
      const client = 'e2e-<img src=x onerror="window.__xss=1">'
      const device = 'e2e-<svg onload="window.__xss=2">'
      await createClientUi(page, client)
      const dialog = await openClient(page, client)
      await addDevice(page, device)
      await expect(dialog.getByRole('heading', { name: client, exact: true })).toBeVisible()
      await deviceCard(page, device).wg.click()
      const config = page.getByRole('dialog', { name: device, exact: true })
      await expect(config.getByRole('heading', { name: device, exact: true })).toBeVisible()
      await expect(config.getByRole('button', { name: 'Скачать', exact: true })).toBeEnabled()
      expect(await page.evaluate(() => Reflect.get(window, '__xss'))).toBeUndefined()
      await expect(page.locator('[onerror], [onload]')).toHaveCount(0)
      await config.getByRole('button', { name: /Close|Закрыть/ }).click()
      await dialog.getByRole('button', { name: /Close|Закрыть/ }).click()
      await expect(page.getByRole('dialog')).toHaveCount(0)
      const clients: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
      const id = clients.find(c => c.name === client)!.id
      expect((await page.request.delete(`/api/clients/${id}`)).ok()).toBe(true)
    })

    for (const from of ['/', '/clients']) test(`после SPA-перехода из ${from} в профиль таймеры раздела и visibilitychange не опрашивают API`, async ({ page }) => {
      await page.clock.install()
      await openHome(page, from)
      await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
      await page.getByRole('menuitem', { name: 'Мой профиль' }).click()
      await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
      await hydrated(page)
      const requests: string[] = []
      page.on('request', (request) => {
        const path = new URL(request.url()).pathname
        // Статус бота опрашивает сам профиль: настройки ботов живут там.
        if (/^\/api\/(clients|routes|ops\/nodes)(\/|$)/.test(path)) requests.push(path)
      })
      await page.clock.fastForward(65_000)
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      await page.waitForLoadState('networkidle')
      expect(requests, 'подписки и интервалы размонтированного раздела должны быть сняты').toEqual([])
    })
  })
}
