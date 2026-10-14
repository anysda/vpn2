import type { Page } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { openHome } from './support/clients'

function botsCard(page: Page) {
  return page.locator('div.card').filter({ has: page.getByRole('heading', { name: 'Бот Telegram', exact: true }) })
}

test.describe('настройки Telegram', () => {
  test('поздний ответ статуса не стирает черновик настроек', async ({ page }) => {
    let hold = false
    let entered!: () => void
    let release!: () => void
    const requested = new Promise<void>(resolve => { entered = resolve })
    const released = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/admin/telegram', async (route) => {
      if (hold) { entered(); await released }
      await route.fulfill({ json: {
        configured: true, running: hold, bot_token_masked: 'masked',
        chat_id: '123', admin_username: 'original',
      } })
    })
    try {
      await openHome(page, '/settings')
      const card = botsCard(page)
      const nick = card.getByLabel('Никнейм админа бота', { exact: true })
      await expect(nick).toHaveValue('original')
      hold = true
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      await requested
      await nick.fill('@draft')
      await card.getByLabel('Chat ID', { exact: true }).fill('321')
      await card.getByLabel('Token', { exact: true }).fill('draft-token')
      release()
      await expect(card.getByText('Онлайн', { exact: true })).toBeVisible()
      await expect(nick).toHaveValue('@draft')
      await expect(card.getByLabel('Chat ID', { exact: true })).toHaveValue('321')
      await expect(card.getByLabel('Token', { exact: true })).toHaveValue('draft-token')
      await expect(card.getByRole('button', { name: 'Сохранить' })).toBeEnabled()
    }
    finally { release() }
  })

  test('никнейм админа сохраняется без @ и возвращается обратно', async ({ page }) => {
    await openHome(page, '/settings')
    const card = botsCard(page)
    await expect(card.getByText(/^(Не настроен|Онлайн|Офлайн)$/)).toBeVisible()
    const save = card.getByRole('button', { name: 'Сохранить' })
    await expect(save).toBeDisabled()

    const nick = card.getByLabel('Никнейм админа бота', { exact: true })
    const original = await nick.inputValue()
    const temp = `e2e_admin_${Date.now().toString(36)}`
    try {
      await nick.fill(`@${temp}`)
      await expect(save).toBeEnabled()
      await save.click()
      await expect(toast(page, 'Настройки бота сохранены')).toBeVisible()
      await expect(nick).toHaveValue(temp)
      await expect(save).toBeDisabled()
      const st = await (await page.request.get('/api/admin/telegram')).json()
      expect(st.admin_username).toBe(temp)
      // Токен наружу только маской.
      expect(JSON.stringify(st)).not.toMatch(/\d{6,}:[\w-]{30,}/)

      await nick.fill(original)
      if (original !== temp) {
        await save.click()
        await expect(toast(page, 'Настройки бота сохранены').last()).toBeVisible()
      }
    }
    finally {
      await page.request.put('/api/admin/telegram', { data: { admin_username: original } })
    }
    const st = await (await page.request.get('/api/admin/telegram')).json()
    expect(st.admin_username).toBe(original)
  })
  // Бейдж — по /health бота, а не по открытому порту: бот с отвергнутым
  // токеном держит порт, но не отвечает (подставной бот в fake-backends так же).
  test('бейдж бота: принятый токен — ОНЛАЙН, отвергнутый — ОФФЛАЙН', async ({ page }) => {
    await openHome(page, '/settings')
    const before = await (await page.request.get('/api/admin/telegram')).json()
    test.skip(before.configured, 'на стенде настоящий токен — маска не даст его вернуть')
    const card = botsCard(page)
    const setToken = async (bot_token: string, chat_id: string) => {
      const res = await page.request.put('/api/admin/telegram', { data: { bot_token, chat_id } })
      expect(res.ok()).toBe(true)
    }
    try {
      await setToken('123456789:E2E_ACCEPTED_aaaaaaaaaaaaaaaaaaaaaaaaaa', '123456789')
      await page.reload()
      await expect(card.getByText('Онлайн', { exact: true })).toBeVisible()
      // Имя бота - из /health (getMe), подписчики и очередь - из базы панели.
      await expect(card.getByText('@demo_vpn_bot', { exact: true })).toBeVisible()
      const st = await (await page.request.get('/api/admin/telegram')).json()
      expect(st).toMatchObject({ running: true, username: 'demo_vpn_bot' })
      for (const key of ['clients_total', 'clients_subscribed', 'queued', 'delivered_day']) expect(st[key]).toEqual(expect.any(Number))
      await expect(card.getByText(`${st.clients_subscribed} из ${st.clients_total}`, { exact: true })).toBeVisible()
      await openHome(page, '/')
      await expect(page.locator('.ov-broadcast .sh .tag')).toHaveText('@demo_vpn_bot')
      await openHome(page, '/settings')

      await setToken('123456789:E2E_REJECTED_bbbbbbbbbbbbbbbbbbbbbbbbbb', '123456789')
      await page.reload()
      await expect(card.getByText('Офлайн', { exact: true })).toBeVisible()
      expect((await (await page.request.get('/api/admin/telegram')).json()).running).toBe(false)
      await expect(card.getByText('@demo_vpn_bot', { exact: true })).toHaveCount(0)
    }
    finally {
      await setToken('', String(before.chat_id ?? ''))
    }
    await page.reload()
    await expect(card.getByText('Не настроен', { exact: true })).toBeVisible()
  })

  test('бот не настроен: новый клиент не встаёт в очередь, рассылка отвечает отказом', async ({ page, guard }) => {
    guard.allow(409, /\/api\/admin\/broadcast$/, 'POST')
    await openHome(page, '/settings')
    test.skip((await (await page.request.get('/api/admin/telegram')).json()).configured, 'нужна панель без бота')
    const name = uniq('nobot')
    const created = await page.request.post('/api/clients', { data: { name } })
    expect(created.ok()).toBe(true)
    const clientId = (await created.json()).id
    try {
      const deliveries = await (await page.request.get('/api/admin/bot-deliveries')).json()
      console.log('без бота, очередь после создания клиента:', JSON.stringify(deliveries.filter((row: { status: string }) => row.status === 'queued')))
      expect(deliveries.filter((row: { label: string }) => row.label === `Уведомление о клиенте «${name}»`)).toEqual([])
      const card = botsCard(page)
      const polled = page.waitForResponse(response => response.url().endsWith('/api/admin/bot-deliveries'))
      await page.reload()
      await polled
      await expect(card.getByText('Не настроен', { exact: true })).toBeVisible()
      await expect(card.locator('.ys div').filter({ hasText: 'В очереди' }).locator('b')).toHaveText('0')

      // Рассылка живёт на Обзоре.
      await openHome(page, '/')
      const bc = page.locator('.ov-broadcast')
      await bc.getByLabel('Сообщение юзерам').fill('e2e без бота')
      await expect(bc.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled()
      await expect(bc.getByText('Сначала настройте Telegram-бота', { exact: true })).toBeVisible()
      // кнопку можно обойти запросом - сервер всё равно отказывает
      const sent = await page.request.post('/api/admin/broadcast', { data: { text: 'e2e без бота' } })
      expect(sent.status()).toBe(409)
      expect((await sent.json()).message).toBe('Telegram-бот не настроен. Рассылка не отправлена.')
      console.log('без бота, рассылка:', sent.status(), (await sent.json()).message)
    }
    finally {
      await page.request.delete(`/api/clients/${clientId}`)
    }
  })
})
