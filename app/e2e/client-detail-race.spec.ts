import { expect, test, uniq } from './support/fixtures'
import { clientCard, createClientUi, openClient, openHome } from './support/clients'

for (const width of [1400, 390]) {
  test.describe(`переключение клиента, ${width}px`, () => {
    test.use({ viewport: { width, height: 844 } })

    test('запоздавший GET закрытой карточки не подменяет открытого клиента', async ({ page }) => {
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
      try {
        await page.getByPlaceholder('Поиск...').fill(first)
        await clientCard(page, first).click()
        await ready
        await page.getByRole('dialog').getByRole('button', { name: /Close|Закрыть/ }).click()
        await expect(page.getByRole('dialog')).toHaveCount(0)
        await openClient(page, second)
      }
      finally {
        release()
      }
      await page.unrouteAll({ behavior: 'wait' })
      // Дать браузеру обработать освобождённый ответ и следующую отрисовку.
      await page.waitForLoadState('networkidle')
      await expect(page.getByRole('dialog').getByLabel('Имя клиента')).toHaveValue(second)
      await expect(page.getByRole('dialog').getByRole('heading', { name: second, exact: true })).toBeVisible()
    })
  })
}
