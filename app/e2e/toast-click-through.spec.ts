import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'

/**
 * Область тостов, раскрытая наведением, выше самих тостов на отступ между ними.
 * Пустая её часть лежит поверх кнопок карточки клиента и не должна забирать клик:
 * иначе «Удалить клиента» под ней молча не нажимается.
 */
test('пустая часть раскрытой области тостов пропускает клик к странице', async ({ page }) => {
  await loginApi(page)
  const name = uniq('toast')
  const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(r.status(), await r.text()).toBe(200)
  const id = (await r.json()).id as number
  try {
    await page.goto('/clients')
    await clientCard(page, name).click()
    const detail = page.getByTestId('client-detail')
    await expect(clientName(detail)).toHaveText(name)
    await detail.getByRole('button', { name: 'Заморозить' }).click()

    const toast = page.locator('ol[data-slot="viewport"] > li').filter({ hasText: 'Отключение выполняется' })
    await expect(toast).toBeVisible()
    await toast.hover()

    // Точка внутри области тостов, но вне любого тоста; есть она только у раскрытой области.
    const gap = () => page.evaluate(() => {
      const ol = document.querySelector('ol[data-slot="viewport"]')!
      const box = ol.getBoundingClientRect()
      const items = [...ol.querySelectorAll(':scope > li')].map(li => li.getBoundingClientRect())
      const x = box.left + box.width / 2
      for (let y = Math.ceil(box.top) + 1; y < box.bottom - 1; y++) {
        if (!items.some(i => y >= i.top && y <= i.bottom)) return { x, y }
      }
      return null
    })
    await expect.poll(gap).not.toBeNull()
    const point = (await gap())!
    const hit = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y)
      return el?.closest('ol[data-slot="viewport"]') ? 'toasts' : 'page'
    }, point)
    expect(hit).toBe('page')
  }
  finally {
    await page.request.delete(`/api/clients/${id}`)
  }
})
