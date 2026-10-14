import { expect, NODES, test } from './support/fixtures'
import { openHome } from './support/clients'

test.describe('Обзор', () => {
  test('рассылки на Обзоре нет: она под списком клиентов', async ({ page }) => {
    await openHome(page)
    await expect(page.locator('.ov-nodes')).toBeVisible()
    await expect(page.locator('.ov-broadcast')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toHaveCount(0)
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
  test('пилюля периода переключает график на неделю и обратно', async ({ page }) => {
    await openHome(page)
    const wave = page.locator('.ov-wave')
    const pill = page.locator('.head .pill')
    await expect(wave.locator('h3')).toHaveText('Трафик за сутки')
    const day = page.waitForResponse(r => r.url().includes('/api/ops/traffic?period=week'))
    await pill.click()
    await day
    await expect(pill).toHaveText('Неделя')
    await expect(wave.locator('h3')).toHaveText('Трафик за неделю')
    await pill.click()
    await expect(pill).toHaveText('Сутки')
    await expect(wave.locator('h3')).toHaveText('Трафик за сутки')
  })

  test('линейка волны показывает трафик за час', async ({ page }) => {
    await openHome(page)
    const wave = page.locator('.ov-wave')
    await expect(wave.locator('.big')).not.toHaveText('—ГБ')
    const box = (await wave.locator('.plot').boundingBox())!
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height / 2)
    await expect(wave).toHaveClass(/\bhv\b/)
    await expect(wave.locator('.qtip')).toContainText(/ГБ за час\d\d:00–\d\d:00/)
  })

  test('активность: «Трафик» показывает ГБ за сутки, «Онлайн» — клиентов в сети', async ({ page }) => {
    await openHome(page)
    const act = page.locator('.ov-activity')
    await expect(act.locator('.agrid i').first()).toBeVisible()
    await expect(act.locator('.aread')).toContainText('в сети')
    await act.getByRole('button', { name: 'Трафик' }).click()
    await expect(act.getByRole('button', { name: 'Трафик' })).toHaveAttribute('aria-pressed', 'true')
    await expect(act.locator('.aread')).toContainText(/ГБ$/)
    // наведение переносит выбор на день
    const cell = act.locator('.agrid i').last()
    await cell.hover()
    await expect(cell).toHaveClass(/\bsel\b/)
    await act.getByRole('button', { name: 'Онлайн' }).click()
    await expect(act.locator('.aread')).toContainText('в сети')
  })
})
