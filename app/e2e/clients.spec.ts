import { expect, test, toast, uniq } from './support/fixtures'
import { clientCard, createClientUi, nextYear, openClient, openHome } from './support/clients'

test.describe('клиенты', () => {
  test('«Отмена» в форме нового клиента ничего не создаёт', async ({ page }) => {
    await openHome(page)
    const name = uniq('cancel')
    await page.getByRole('button', { name: 'Новый клиент' }).click()
    await page.getByPlaceholder('Иван Иванов').fill(name)
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Новый клиент' })).toBeVisible()
    const list: { name: string }[] = await (await page.request.get('/api/clients')).json()
    expect(list.map(c => c.name)).not.toContain(name)
  })

  test('создать, найти, переименовать, срок, заморозка, удалить', async ({ page }) => {
    await openHome(page)
    const name = uniq('client')
    const expires = nextYear()
    await createClientUi(page, name, { expires })
    await page.getByPlaceholder('Поиск...').fill(name)
    await expect(clientCard(page, name)).toHaveCount(1)
    await expect(clientCard(page, name)).toContainText('Активен')

    const dlg = await openClient(page, name)
    await expect(dlg.getByPlaceholder('ДД.ММ.ГГГГ')).toHaveValue(expires)
    await expect(dlg.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()

    // Переименование по Enter.
    const renamed = `${name}-r`
    await dlg.getByLabel('Имя клиента').fill(renamed)
    await dlg.getByLabel('Имя клиента').press('Enter')
    await expect(toast(page, 'Имя сохранено')).toBeVisible()
    await expect(page.getByRole('dialog').getByRole('heading', { name: renamed })).toBeVisible()

    // Срок: очистить = бессрочно.
    const date = dlg.getByPlaceholder('ДД.ММ.ГГГГ')
    await date.fill('')
    await date.press('Tab')
    await expect(toast(page, 'Срок действия сохранён')).toBeVisible()

    // Безлимит.
    await dlg.getByRole('button', { name: 'Безлимитно' }).click()
    await expect(dlg.getByText('лимит снят (∞)')).toBeVisible()

    // Заморозить и разморозить.
    await dlg.getByRole('button', { name: 'Заморозить' }).click()
    await expect(toast(page, 'Клиент заморожен')).toBeVisible()
    await dlg.getByRole('button', { name: 'Разморозить' }).click()
    await expect(toast(page, 'Клиент разморожен')).toBeVisible()

    // «Отмена» в подтверждениях.
    await dlg.getByRole('button', { name: 'Перевыпустить все ключи' }).click()
    const reissue = page.getByRole('dialog', { name: 'Перевыпустить все ключи?' })
    await reissue.getByRole('button', { name: 'Отмена' }).click()
    await expect(reissue).toBeHidden()

    await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
    const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
    await del.getByRole('button', { name: 'Отмена' }).click()
    await expect(del).toBeHidden()
    let list: { name: string }[] = await (await page.request.get('/api/clients')).json()
    expect(list.map(c => c.name)).toContain(renamed)

    await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
    await del.getByRole('button', { name: 'Удалить' }).click()
    await expect(toast(page, `Клиент «${renamed}» удалён`)).toBeVisible()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(clientCard(page, renamed)).toHaveCount(0)
    list = await (await page.request.get('/api/clients')).json()
    expect(list.map(c => c.name)).not.toContain(renamed)
  })
})
