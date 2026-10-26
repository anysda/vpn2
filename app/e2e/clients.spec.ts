import { expect, test, toast, uniq } from './support/fixtures'
import { clientCard, clientName, createClientUi, nextYear, openClient, openHome } from './support/clients'
import { rects } from './support/geometry'

test.describe('клиенты', () => {
  test('«Отмена» в форме нового клиента ничего не создаёт', async ({ page }) => {
    await openHome(page, '/clients')
    const name = uniq('cancel')
    await page.getByRole('button', { name: 'Новый клиент' }).click()
    await page.getByPlaceholder('Иван Иванов').fill(name)
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Новый клиент' })).toBeVisible()
    const list: { name: string }[] = await (await page.request.get('/api/clients')).json()
    expect(list.map(c => c.name)).not.toContain(name)
  })

  test('создать, найти, переименовать, срок, заморозка, удалить', async ({ page }) => {
    await openHome(page, '/clients')
    const name = uniq('client')
    const expires = nextYear()
    const creating = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients')
    await createClientUi(page, name, { expires })
    const created = await creating
    expect(created.status()).toBe(200)
    const client = await created.json()
    expect(client.expiresAt).toBeTruthy()
    await page.getByPlaceholder('Поиск...').fill(name)
    await expect(clientCard(page, name)).toHaveCount(1)
    // Бейдж подписки — только «Заморожен»; у нового клиента вместо него точка «в сети».
    await expect(clientCard(page, name)).not.toContainText('Заморожен')
    await expect(clientCard(page, name).getByTestId('client-online')).toBeVisible()

    const dlg = await openClient(page, name)
    await expect(dlg.getByLabel('Истекает')).toHaveValue(expires)
    await expect(dlg.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()

    // Переименование по Enter.
    const renamed = `${name}-r`
    await clientName(dlg).fill(renamed)
    await clientName(dlg).press('Enter')
    await expect(toast(page, 'Имя сохранено')).toBeVisible()
    await expect(clientName(dlg)).toHaveText(renamed)
    await expect(clientCard(page, renamed)).toHaveCount(1)

    // Срок: очистить = бессрочно.
    const date = dlg.getByLabel('Истекает')
    // Сначала сервер завершает синхронизацию VPN, затем UI показывает toast.
    // Его короткое ожидание не должно включать всю длительность PATCH.
    const expirySaved = page.waitForResponse(r => r.request().method() === 'PATCH'
      && new URL(r.url()).pathname === `/api/clients/${client.id}`
      && r.request().postDataJSON()?.expiresAt === null)
    await date.fill('')
    await date.press('Tab')
    expect((await expirySaved).status()).toBe(202)
    await expect(toast(page, 'Срок действия сохранён')).toBeVisible()
    const saved = await page.request.get(`/api/clients/${client.id}`)
    expect(saved.status()).toBe(200)
    expect((await saved.json()).expiresAt, 'снятый срок должен сохраниться на сервере').toBeNull()
    await page.reload()
    await openClient(page, renamed)
    await expect(date).toHaveValue('')

    // Безлимит.
    await dlg.getByRole('button', { name: 'Безлимитно' }).click()
    await expect(dlg.getByText('лимит снят (∞)')).toBeVisible()

    // Заморозить и разморозить.
    await dlg.getByRole('button', { name: 'Заморозить' }).click()
    await expect(toast(page, 'Отключение выполняется')).toBeVisible()
    await expect(dlg.getByRole('status')).toContainText('Доступ отключён', { timeout: 45_000 })
    await expect(clientCard(page, renamed)).toContainText('Доступ отключён')
    await dlg.getByRole('button', { name: 'Разморозить' }).click()
    await expect(toast(page, 'Включение выполняется')).toBeVisible()
    await expect(dlg.getByRole('status')).toContainText('Доступ включён', { timeout: 45_000 })
    await expect(clientCard(page, renamed)).not.toContainText('Доступ отключён')

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
    await expect(toast(page, 'Отключение выполняется')).toBeVisible()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(clientCard(page, renamed)).toHaveCount(0, { timeout: 45_000 })
    list = await (await page.request.get('/api/clients')).json()
    expect(list.map(c => c.name)).not.toContain(renamed)
  })

  // VPN2-69: 76 мест бросали код без текста, и тост, и бот показывали
  // «not_found», «invalid_id». Код остаётся в statusMessage, текст — из словаря.
  test('ошибки API без своего текста приходят по-русски, код в statusMessage', async ({ page, request }) => {
    await openHome(page, '/clients')
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

test.describe('рассылка под списком клиентов', () => {
  test('карточка рассылки стоит под списком клиентов', async ({ page }) => {
    await openHome(page, '/clients')
    const card = page.locator('.ov-broadcast')
    await expect(card.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toBeVisible()
    // В колонке списка ниже самого списка, не в карточке клиента.
    const [listBox, cardBox] = await rects(page, '.ccol .clist', '.ccol .ov-broadcast')
    expect(cardBox.y).toBeGreaterThanOrEqual(listBox.y + listBox.height)
    await expect(page.locator('.ccol .ov-broadcast')).toHaveCount(1)
    await expect(page.getByTestId('client-detail').locator('.ov-broadcast')).toHaveCount(0)
  })

  test('без настроенного бота рассылку не отправить, видна подсказка', async ({ page }) => {
    await page.route('**/api/admin/telegram', async (route) => {
      const res = await route.fetch()
      await route.fulfill({ response: res, json: { ...await res.json(), configured: false } })
    })
    await openHome(page, '/clients')
    const card = page.locator('.ov-broadcast')
    await card.getByLabel('Сообщение юзерам').fill('e2e без бота')
    await expect(card.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled()
    await expect(card.getByText('Сначала настройте Telegram-бота', { exact: true })).toBeVisible()
    // опрос может прийти под конец теста — не дать перехвату упасть уже в следующем
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  })

  test('в шапке рассылки - @имя бота, пока бот его назвал', async ({ page }) => {
    let username = 'e2e_named_bot'
    await page.route('**/api/admin/telegram', async (route) => {
      const res = await route.fetch()
      await route.fulfill({ response: res, json: { ...await res.json(), configured: true, running: Boolean(username), username } })
    })
    await openHome(page, '/clients')
    const card = page.locator('.ov-broadcast')
    await expect(card.locator('.sh .tag')).toHaveText('@e2e_named_bot')
    username = ''
    await page.reload()
    await expect(card.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toBeVisible()
    await expect(card.locator('.sh .tag')).toHaveCount(0)
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  })

  test('рассылка из-под списка клиентов уходит боту', async ({ page }) => {
    await openHome(page, '/clients')
    test.skip(!(await (await page.request.get('/api/admin/telegram')).json()).configured, 'нужна панель с ботом')
    const card = page.locator('.ov-broadcast')
    await expect(card.getByText('Сначала настройте Telegram-бота')).toHaveCount(0)
    const send = card.getByRole('button', { name: 'Отправить', exact: true })
    await expect(send).toBeDisabled()
    await card.getByLabel('Сообщение юзерам').fill('e2e рассылка со страницы клиентов')
    const posted = page.waitForRequest(r => r.url().endsWith('/api/admin/broadcast') && r.method() === 'POST')
    await send.click()
    expect((await posted).postDataJSON()).toEqual({ text: 'e2e рассылка со страницы клиентов' })
    await expect(card.getByLabel('Сообщение юзерам')).toHaveValue('')
  })
})
