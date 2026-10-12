import { downloadText, expect, hydrated, loginApi, PUBLIC_HOST, test, uniq } from './support/fixtures'
import { deviceCard, openClient } from './support/clients'

const warningText = 'Уберите у домена IPv6-запись (AAAA), иначе у клиентов на Linux VPN может не заработать.'

for (const dns of ['aaaa', 'a-only', 'unavailable']) {
  test(`WG: предупреждение в диалоге при DNS ${dns}, профиль доступен`, async ({ page }) => {
    await loginApi(page)
    const name = uniq('wg-dns')
    const client = await (await page.request.post('/api/clients', { data: { name } })).json()
    await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
    await page.route('**/api/wg-endpoint-warning', route => route.fulfill({
      json: { hasAAAA: dns === 'aaaa' },
    }))
    await page.goto('/')
    await hydrated(page)
    await openClient(page, name)
    const checked = page.waitForResponse('**/api/wg-endpoint-warning')
    await deviceCard(page, 'phone').wg.click()
    await checked
    const dialog = page.getByRole('dialog', { name: 'phone' })
    const warning = dialog.getByText(warningText, { exact: true })
    if (dns === 'aaaa') await expect(warning).toBeVisible()
    else await expect(warning).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: 'Копировать', exact: true })).toBeEnabled()
    const downloaded = page.waitForEvent('download')
    await dialog.getByRole('button', { name: 'Скачать', exact: true }).click()
    expect(await downloadText(await downloaded)).toContain(`Endpoint = ${PUBLIC_HOST}:51820\n`)
  })
}

test('WG: ожидание DNS не задерживает профиль, повторное открытие очищает предупреждение', async ({ page }) => {
  await loginApi(page)
  const name = uniq('wg-dns-delay')
  const client = await (await page.request.post('/api/clients', { data: { name } })).json()
  await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
  let pending = false
  await page.route('**/api/wg-endpoint-warning', async (route) => {
    if (pending) return // запрос висит до клиентского таймаута
    await route.fulfill({ json: { hasAAAA: true } })
  })
  await page.goto('/')
  await hydrated(page)
  await openClient(page, name)
  await deviceCard(page, 'phone').wg.click()
  const dialog = page.getByRole('dialog', { name: 'phone' })
  await expect(dialog.getByText(warningText)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  pending = true
  await deviceCard(page, 'phone').wg.click()
  await expect(dialog.getByText(warningText)).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: 'Скачать', exact: true })).toBeEnabled()
})
