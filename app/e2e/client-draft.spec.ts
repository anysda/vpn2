import { expect, test, toast, uniq } from './support/fixtures'
import { createClientUi, nextYear, openClient, openHome } from './support/clients'

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
      const input = dialog.getByLabel('Имя клиента')
      guard.allow(409, /\/api\/clients\/\d+$/, 'PATCH')
      await input.fill(taken)
      await input.press('Enter')
      await expect(toast(page, `Клиент с именем «${taken}» уже существует`)).toBeVisible()
      await expect(input).toHaveValue(taken)
      await expect(dialog.getByRole('heading', { name, exact: true })).toBeVisible()
      await input.fill(`${taken}-fixed`)
      await input.press('Enter')
      await expect(dialog.getByRole('heading', { name: `${taken}-fixed`, exact: true })).toBeVisible()
    })

    test('422 при сохранении срока сохраняет дату для исправления', async ({ page, guard }) => {
      await openHome(page, '/clients')
      const name = uniq('expiry-draft')
      await createClientUi(page, name)
      const dialog = await openClient(page, name)
      const input = dialog.getByPlaceholder('ДД.ММ.ГГГГ')
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
