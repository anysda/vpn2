import { expect, hydrated, loginApi, test, toast } from './support/fixtures'

const defaults = { left: ['traffic', 'nodes', 'activity'], right: [], hidden: [] }

test('«Настроить экран»: скрытый блок пропадает с Обзора и возвращается сбросом', async ({ page }) => {
  await loginApi(page)
  try {
    await page.goto('/screen')
    await hydrated(page)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(['Настроить экран'])
    const shown = page.getByRole('list', { name: 'На обзоре' })
    const hidden = page.getByRole('list', { name: 'Скрыто' })
    await expect(shown.getByRole('listitem')).toHaveText([
      /События, трафик, выходы и DNS/, /Узлы/, /Активность и клиенты/,
    ])
    await expect(page.getByRole('button', { name: 'Сбросить по умолчанию' })).toBeDisabled()

    await shown.getByRole('listitem').filter({ hasText: 'События, трафик, выходы и DNS' }).dragTo(hidden)
    await expect(hidden.getByRole('listitem')).toHaveText([/События, трафик, выходы и DNS/])
    await expect(shown.getByRole('listitem')).toHaveCount(2)
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await expect(toast(page, 'Экран сохранён')).toBeVisible()
    await expect(page).toHaveURL(/\/$/)

    // После перезагрузки раскладка из базы: первого ряда с лентой на Обзоре нет.
    await page.reload()
    await hydrated(page)
    await expect(page.locator('[data-blk="nodes"]')).toBeAttached()
    await expect(page.locator('[data-blk="traffic"]')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'События', exact: true })).toHaveCount(0)
    expect(await (await page.request.get('/api/me/layout')).json()).toMatchObject({ hidden: ['traffic'] })

    await page.goto('/screen')
    await hydrated(page)
    await page.getByRole('button', { name: 'Сбросить по умолчанию' }).click()
    await expect(hidden.getByRole('listitem')).toHaveCount(0)
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('[data-blk="traffic"]')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'События', exact: true })).toBeVisible()
  }
  finally {
    await page.request.put('/api/me/layout', { data: defaults })
  }
})
