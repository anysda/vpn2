import { expect, NODES, test } from './support/fixtures'
import { openHome } from './support/clients'

test.describe('Обзор', () => {
  test('без настроенного бота рассылку не отправить, видна подсказка', async ({ page }) => {
    await page.route('**/api/admin/telegram', async (route) => {
      const res = await route.fetch()
      await route.fulfill({ response: res, json: { ...await res.json(), configured: false } })
    })
    await openHome(page)
    const card = page.locator('.ov-broadcast')
    await card.getByLabel('Сообщение юзерам').fill('e2e без бота')
    await expect(card.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled()
    await expect(card.getByText('Сначала настройте Telegram-бота', { exact: true })).toBeVisible()
    // опрос может прийти под конец теста — не дать перехвату упасть уже в следующем
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  })

  test('рассылка с Обзора уходит боту', async ({ page }) => {
    await openHome(page)
    test.skip(!(await (await page.request.get('/api/admin/telegram')).json()).configured, 'нужна панель с ботом')
    const card = page.locator('.ov-broadcast')
    await expect(card.getByText('Сначала настройте Telegram-бота')).toHaveCount(0)
    const send = card.getByRole('button', { name: 'Отправить', exact: true })
    await expect(send).toBeDisabled()
    await card.getByLabel('Сообщение юзерам').fill('e2e рассылка с Обзора')
    const posted = page.waitForRequest(r => r.url().endsWith('/api/admin/broadcast') && r.method() === 'POST')
    await send.click()
    expect((await posted).postDataJSON()).toEqual({ text: 'e2e рассылка с Обзора' })
    await expect(card.getByLabel('Сообщение юзерам')).toHaveValue('')
  })

  test('экзит выключается и включается из окна узла', async ({ page }) => {
    const exits = NODES.filter(t => t !== 'ru')
    test.skip(exits.length < 2, 'нужно два экзита')
    const tag = exits[0]!
    await openHome(page)
    const card = page.locator(`.ov-nodes .node[data-tag="${tag}"]`)
    await card.click()
    const dialog = page.getByRole('dialog')
    const toggle = dialog.getByRole('switch')
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    const node = (await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, active?: boolean, activeDevices?: number }>)
      .find(n => n.tag === tag)!
    await toggle.click()
    // через экзит идёт трафик — сначала вопрос
    if (node.active || node.activeDevices) {
      await expect(dialog.getByText(`Через ${tag.toUpperCase()} сейчас идёт трафик.`, { exact: false })).toBeVisible()
      await dialog.getByRole('button', { name: 'Выключить', exact: true }).click()
      await expect(dialog).toHaveCount(0)
      await card.click()
    }
    await expect(card.locator('.ft .tag')).toHaveText('ВЫКЛ')
    expect((await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, disabled: boolean }>)
      .find(n => n.tag === tag)?.disabled).toBe(true)

    await dialog.getByRole('switch').click()
    await expect(card.locator('.ft .tag')).not.toHaveText('ВЫКЛ')
    await expect(dialog.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
  })
})
