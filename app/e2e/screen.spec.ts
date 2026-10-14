import { expect, hydrated, loginApi, test, toast } from './support/fixtures'

const defaults = { left: ['traffic', 'nodes', 'activity', 'events'], right: [], hidden: [] }

test('«Настроить экран»: скрытый блок пропадает с Обзора и возвращается сбросом', async ({ page }) => {
  await loginApi(page)
  try {
    await page.goto('/screen')
    await hydrated(page)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(['Настроить экран'])
    const shown = page.getByRole('list', { name: 'На обзоре' })
    const hidden = page.getByRole('list', { name: 'Скрыто' })
    await expect(shown.getByRole('listitem')).toHaveText([
      /Трафик, выходы и DNS/, /Узлы/, /Активность и клиенты/, /Рассылка и события/,
    ])
    await expect(page.getByRole('button', { name: 'Сбросить по умолчанию' })).toBeDisabled()

    await shown.getByRole('listitem').filter({ hasText: 'Рассылка и события' }).dragTo(hidden)
    await expect(hidden.getByRole('listitem')).toHaveText([/Рассылка и события/])
    await expect(shown.getByRole('listitem')).toHaveCount(3)
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await expect(toast(page, 'Экран сохранён')).toBeVisible()
    await expect(page).toHaveURL(/\/$/)

    // После перезагрузки раскладка из базы: рассылки на Обзоре нет.
    await page.reload()
    await hydrated(page)
    await expect(page.locator('[data-blk="nodes"]')).toBeAttached()
    await expect(page.locator('[data-blk="events"]')).toHaveCount(0)
    await expect(page.getByLabel('Сообщение юзерам')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'События', exact: true })).toHaveCount(0)
    expect(await (await page.request.get('/api/me/layout')).json()).toMatchObject({ hidden: ['events'] })

    await page.goto('/screen')
    await hydrated(page)
    await page.getByRole('button', { name: 'Сбросить по умолчанию' }).click()
    await expect(hidden.getByRole('listitem')).toHaveCount(0)
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByLabel('Сообщение юзерам')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'События', exact: true })).toBeVisible()
  }
  finally {
    await page.request.put('/api/me/layout', { data: defaults })
  }
})
