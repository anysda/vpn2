import type { Page } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { clientName, createClientUi, nextYear, openClient, openHome } from './support/clients'

async function clientId(page: Page, name: string) {
  const list: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
  return list.find(c => c.name === name)!.id
}

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
      const input = clientName(dialog)
      guard.allow(409, /\/api\/clients\/\d+$/, 'PATCH')
      await input.fill(taken)
      await input.press('Enter')
      await expect(toast(page, `Клиент с именем «${taken}» уже существует`)).toBeVisible()
      await expect(input).toHaveText(taken)
      const kept: { name: string } = await (await page.request.get(`/api/clients/${await clientId(page, name)}`)).json()
      expect(kept.name).toBe(name)
      await input.fill(`${taken}-fixed`)
      await input.press('Enter')
      await expect(toast(page, 'Имя сохранено')).toBeVisible()
      await expect(input).toHaveText(`${taken}-fixed`)
    })

    test('422 при сохранении срока сохраняет дату для исправления', async ({ page, guard }) => {
      await openHome(page, '/clients')
      const name = uniq('expiry-draft')
      await createClientUi(page, name)
      const dialog = await openClient(page, name)
      const input = dialog.getByLabel('Истекает')
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
