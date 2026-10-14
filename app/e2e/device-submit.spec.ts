import { expect, test, toast, uniq } from './support/fixtures'
import { addDeviceButton, createClientUi, openClient, openHome } from './support/clients'

for (const width of [1400, 390]) {
  test.describe(`отправка девайса, ${width}px`, () => {
    test.use({ viewport: { width, height: 844 } })

    test('повторный Enter во время запроса не отправляет второй POST', async ({ page, guard }) => {
      await openHome(page, '/clients')
      const name = uniq('submit')
      await createClientUi(page, name)
      await openClient(page, name)
      await addDeviceButton(page).click()
      const dialog = page.getByRole('dialog', { name: 'Новый девайс' })
      const input = dialog.getByPlaceholder('напр. iPhone')
      await input.fill('e2e-phone')

      let posts = 0
      let release!: () => void
      const held = new Promise<void>((resolve) => { release = resolve })
      let created!: () => void
      const ready = new Promise<void>((resolve) => { created = resolve })
      guard.allow(409, /\/devices$/, 'POST')
      await page.route('**/api/clients/*/devices', async (route) => {
        if (route.request().method() !== 'POST') return route.continue()
        posts++
        const response = await route.fetch()
        created()
        await held
        await route.fulfill({ response })
      })
      try {
        await input.press('Enter')
        await ready
        await input.press('Enter')
      }
      finally {
        release()
      }
      await expect(toast(page, 'Девайс «e2e-phone» добавлен')).toBeVisible()
      await expect(dialog).toBeHidden()
      await page.unrouteAll({ behavior: 'wait' })
      expect(posts, 'одна операция пользователя должна отправить один POST').toBe(1)
    })
  })
}
