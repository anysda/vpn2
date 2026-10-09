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
    // Бейдж подписки — только «Заморожен»; у нового клиента вместо него точка «в сети».
    await expect(clientCard(page, name)).not.toContainText('Заморожен')
    await expect(clientCard(page, name).getByTestId('client-online')).toBeVisible()

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
    await expect(clientCard(page, renamed)).toContainText('Заморожен')
    await dlg.getByRole('button', { name: 'Разморозить' }).click()
    await expect(toast(page, 'Клиент разморожен')).toBeVisible()
    await expect(clientCard(page, renamed)).not.toContainText('Заморожен')

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

  // VPN2-69: 76 мест бросали код без текста, и тост, и бот показывали
  // «not_found», «invalid_id». Код остаётся в statusMessage, текст — из словаря.
  test('ошибки API без своего текста приходят по-русски, код в statusMessage', async ({ page, request }) => {
    await openHome(page)
    const missing = await page.request.get('/api/clients/999999999')
    expect(missing.status()).toBe(404)
    expect(await missing.json()).toMatchObject({ statusMessage: 'not_found', message: 'Не найдено' })
    const bad = await page.request.get('/api/clients/abc')
    expect(bad.status()).toBe(400)
    expect(await bad.json()).toMatchObject({ statusMessage: 'invalid_id', message: 'Неверный идентификатор' })
    // Без сессии.
    const anon = await request.get('/api/clients')
    expect(anon.status()).toBe(401)
    expect(await anon.json()).toMatchObject({ statusMessage: 'Unauthorized', message: 'Нужно войти' })
  })
})
