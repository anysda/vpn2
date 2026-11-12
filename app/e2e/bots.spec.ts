import type { Page } from '@playwright/test'
import { expect, hydrated, toast, uniq, loginApi, test } from './support/fixtures'
import { openHome } from './support/clients'
import { container, inside } from './support/openvpn-ca'
import { checkBotQueue } from './support/bot-queue'

test.describe('bots', () => {
  function botsCard(page: Page) {
    return page.locator('div.card').filter({ has: page.getByRole('heading', { name: 'Бот Telegram', exact: true }) })
  }

  test.describe('настройки Telegram', () => {
    test('статус бота панель запрашивает один раз на страницу', async ({ page }) => {
      let hits = 0
      page.on('request', (r) => {
        if (r.method() === 'GET' && new URL(r.url()).pathname === '/api/admin/telegram') hits++
      })
      await openHome(page, '/settings')
      await expect(botsCard(page).getByText(/^(Не настроен|Онлайн|Офлайн)$/)).toBeVisible()
      await page.waitForLoadState('networkidle')
      expect(hits).toBe(1)

      // Список клиентов: форма нового клиента, панель клиента и окно конфига читают один и тот же статус.
      hits = 0
      await page.goto('/')
      await hydrated(page)
      await page.waitForLoadState('networkidle')
      expect(hits).toBeLessThanOrEqual(1)
    })

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
        await openHome(page, '/clients')
        await expect(page.getByTestId('ov-broadcast').locator('.sh .tag')).toHaveText('@demo_vpn_bot')
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

        // Рассылка живёт под списком клиентов.
        await openHome(page, '/clients')
        const bc = page.getByTestId('ov-broadcast')
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
})

test.describe('bot-delivery-ui', () => {
  test('очередь: короткий тост один раз за сессию вкладки', async ({ page }) => {
    await checkBotQueue(page)
  })

  test('окончательная ошибка доставки гаснет сама, подробности остаются в уведомлениях', async ({ page }) => {
    let failed = false
    await page.route('**/api/admin/bot-deliveries', route => route.fulfill({ json: failed
      ? [{ id: 'failed-toast', label: 'Рассылка', status: 'failed', remaining: 0 }]
      : [] }))
    await openHome(page)
    await page.waitForResponse('**/api/admin/bot-deliveries')
    failed = true
    const title = 'Рассылка: доставка не удалась. Повторов не будет.'
    const errorToast = toast(page, title)
    await expect(errorToast).toBeVisible()
    await page.mouse.move(0, 0)
    await expect(errorToast, 'окончательная ошибка тоже не висит вечно').toHaveCount(0)
    await page.getByRole('button', { name: 'Уведомления: 1', exact: true }).click()
    await expect(page.getByRole('dialog').getByText(title, { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    await page.reload()
    await page.getByRole('button', { name: 'Уведомления: 1', exact: true }).click()
    await expect(page.getByRole('dialog').getByText(title, { exact: true })).toBeVisible()
    await expect(errorToast, 'история ошибок не объявляется повторно').toHaveCount(0)
    console.log('Рассылка: доставка не удалась. Повторов не будет. — тост погас сам, история сохранена')
  })

  test.describe('время в строках уведомлений', () => {
    // Часовой пояс браузера нарочно не московский: время панели всегда МСК.
    test.use({ timezoneId: 'Asia/Vladivostok' })
    test('сегодня и вчера по МСК, как в макете', async ({ page }) => {
      await page.clock.setFixedTime(new Date('2026-10-14T11:31:30Z'))
      // Событие «сегодня» по часам сервера: лента главной рендерится на сервере,
      // часы браузера стоят на 14.10 — гидратация всё равно обязана сойтись.
      await loginApi(page)
      const created = await page.request.post('/api/clients', { data: { name: uniq('clock') } })
      expect(created.status()).toBe(200)
      const { id } = await created.json()
      await page.route('**/api/admin/bot-deliveries', route => route.fulfill({ json: [
        { id: 'yesterday', label: 'Рассылка', status: 'failed', remaining: 0, created: Date.parse('2026-10-13T20:30:00Z') },
        { id: 'after-midnight', label: 'Уведомление клиенту «Ночь»', status: 'failed', remaining: 0, created: Date.parse('2026-10-13T21:05:00Z') },
        { id: 'today', label: 'Уведомление клиенту «День»', status: 'queued', remaining: 2, created: Date.parse('2026-10-14T11:31:00Z') },
      ] }))
      await openHome(page)
      await page.getByRole('button', { name: 'Уведомления: 3', exact: true }).click()
      const dialog = page.getByRole('dialog').filter({ hasText: 'Доставка уведомлений' })
      const row = (text: string) => dialog.getByTestId('bell-row').filter({ hasText: text })
      await expect(row('Рассылка: доставка не удалась').locator('small')).toHaveText('вчера 23:30')
      await expect(row('«Ночь»').locator('small')).toHaveText('сегодня 00:05')
      await expect(row('«День»: не отправлено. Ещё попыток: 2.').locator('small')).toHaveText('сегодня 14:31')
      console.log('Колокольчик:', (await dialog.getByTestId('bell-row').locator('small').allTextContents()).join(' | '))
      expect((await page.request.delete(`/api/clients/${id}`)).status()).toBe(202)
    })
  })

  test('недоступный бот: тост клиента, рассылки, восстановление и окончательная ошибка', async ({ page, request }) => {
    test.skip(!container, 'нужен одноразовый локальный контейнер с подставным ботом')
    const anonymous = await page.request.get('/api/admin/bot-deliveries')
    expect(anonymous.ok()).toBe(true)
    expect(await anonymous.json()).toEqual([])
    // Рассылка - под списком клиентов, очередь бота - в карточке «Настроек».
    await openHome(page, '/clients')
    const original = await (await page.request.get('/api/admin/telegram')).json()
    test.skip(original.configured, 'настоящие настройки бота менять нельзя')
    const name = uniq('delivery')
    let clientId: number | undefined
    const token = async (available: boolean) => {
      const response = await page.request.put('/api/admin/telegram', {
        data: { bot_token: available ? '123456789:E2E_ACCEPTED_aaaaaaaaaaaaaaaaaaaaaaaaaa' : '123456789:E2E_UNAVAILABLE_aaaaaaaaaaaaaaaaaaaaaaa', chat_id: '123456789' },
      })
      expect(response.ok()).toBe(true)
    }
    const due = (id: string, exhausted = false) => inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    db.exec('PRAGMA busy_timeout=5000');
    const result = db.prepare('UPDATE bot_outbox SET due = 0, attempts = ? WHERE id = ?').run(JSON.parse(process.argv[2]) ? 5 : 1, JSON.parse(process.argv[1]));
    if (result.changes !== 1) throw new Error('test delivery not found');
  `, id, exhausted)
    try {
      // Это одноразовый контейнер без настоящего бота (проверено выше).
      // Убираем уведомления других сценариев: попытка «ускорить» их через due
      // гонялась с уже выполняющимся POST, который возвращал due в будущее.
      // Доставку пяти НОВЫХ уведомлений ниже проверяем настоящим обработчиком.
      inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec('PRAGMA busy_timeout=5000; DELETE FROM bot_outbox');
      db.close();
    `)
      await token(false)
      const created = await page.request.post('/api/clients', { data: { name } })
      expect(created.ok()).toBe(true)
      clientId = (await created.json()).id
      await expect(toast(page, 'Бот недоступен, 1 уведомление в очереди')).toBeVisible()
      await page.getByRole('button', { name: 'Уведомления: 1', exact: true }).click()
      await expect(page.getByRole('dialog').getByText('Бот недоступен, 1 уведомление в очереди', { exact: true })).toBeVisible()
      await page.keyboard.press('Escape')
      console.log('Реальная очередь: Бот недоступен, 1 уведомление в очереди — тост, «Уведомления»')
      inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec('PRAGMA busy_timeout=5000');
      db.prepare('UPDATE clients SET tg_chat_id = 123456789 WHERE id = ?').run(JSON.parse(process.argv[1]));
    `, clientId)
      const changed = await page.request.patch(`/api/clients/${clientId}`, { data: { deviceLimit: 4 } })
      expect(changed.ok()).toBe(true)
      await expect(page.getByRole('button', { name: 'Уведомления: 2', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Уведомления: 2', exact: true }).click()
      await expect(page.getByRole('dialog').getByText('Бот недоступен, 2 уведомления в очереди', { exact: true })).toBeVisible()
      await page.keyboard.press('Escape')
      console.log('Реальная очередь: Бот недоступен, 2 уведомления в очереди — «Уведомления»')

      for (let n = 3; n <= 5; n++) {
        await page.getByLabel('Сообщение юзерам').fill(`e2e delivery probe ${n}`)
        await page.getByRole('button', { name: 'Отправить', exact: true }).click()
        await expect(page.getByRole('button', { name: `Уведомления: ${n}`, exact: true })).toBeVisible()
      }
      const queueToasts = page.getByRole('region', { includeHidden: true }).locator('ol > li').filter({ hasText: /Бот недоступен|не отправлено/ })
      await page.mouse.move(0, 0)
      await expect(queueToasts, 'реальная очередь остаётся, короткий тост гаснет').toHaveCount(0)
      await openHome(page, '/settings')
      const bot = page.locator('div.card').filter({ has: page.getByRole('heading', { name: 'Бот Telegram', exact: true }) })
      await expect(bot.getByText('Офлайн', { exact: true })).toBeVisible()
      await expect(bot.locator('.ys div').filter({ hasText: 'В очереди' }).locator('b')).toHaveText('5')
      await page.getByRole('button', { name: 'Уведомления: 5', exact: true }).click()
      await expect(page.getByRole('dialog').getByText('Бот недоступен, 5 уведомлений в очереди', { exact: true })).toBeVisible()
      await page.keyboard.press('Escape')
      console.log('Реальная очередь: Бот недоступен, 5 уведомлений в очереди — в «Уведомлениях» и карточке бота; тостов: 0')
      await expect(toast(page, /^Отправлено:/)).toHaveCount(0)
      const statuses = await (await page.request.get('/api/admin/bot-deliveries')).json()
      expect(await (await request.get('/api/admin/bot-deliveries')).json(), 'без сессии история недоступна').toEqual([])
      const broadcast = statuses.find((row: { label: string }) => row.label === 'Рассылка')
      expect(broadcast).toMatchObject({ status: 'queued', remaining: 5 })
      expect(broadcast.created, 'время постановки в очередь, мс').toBeGreaterThan(Date.now() - 5 * 60_000)
      expect(broadcast.created).toBeLessThanOrEqual(Date.now() + 60_000)
      await token(true)
      for (const row of statuses.filter((row: { status: string }) => row.status === 'queued')) due(row.id)
      try {
        await expect.poll(async () => (await (await page.request.get('/api/admin/bot-deliveries')).json())
          .filter((row: { status: string }) => row.status === 'queued').length).toBe(0)
      }
      catch (error) {
        console.log(inside(`
        import { DatabaseSync } from 'node:sqlite';
        const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
        db.exec('PRAGMA busy_timeout = 10000');
        console.log(JSON.stringify(db.prepare('SELECT id, status, attempts, due FROM bot_outbox').all()));
        console.log('bot health', (await fetch('http://127.0.0.1:8877/health')).status);
      `))
        throw error
      }
      const accepted = JSON.parse(inside(`console.log(JSON.stringify(await (await fetch('http://127.0.0.1:8877/events')).json()))`))
      expect(accepted.filter((row: { deliveryId: string }) => row.deliveryId === broadcast.id)).toHaveLength(1)
      await expect(queueToasts).toHaveCount(0)
      console.log('Реальная очередь: бот вернулся, очередь доставлена — тостов: 0')

      await token(false)
      const failed = await page.request.patch(`/api/clients/${clientId}`, { data: { deviceLimit: 5 } })
      expect(failed.ok()).toBe(true)
      const pending = (await (await page.request.get('/api/admin/bot-deliveries')).json())
        .find((row: { status: string }) => row.status === 'queued')
      due(pending.id, true)
      await expect(toast(page, `Уведомление клиенту «${name}»: доставка не удалась. Повторов не будет.`)).toBeVisible()
      await page.reload()
      await page.getByRole('button', { name: /^Уведомления:/ }).click()
      // После перезагрузки тоста нет: строка - в списке «Уведомлений», со временем под текстом.
      await expect(page.getByRole('dialog').getByTestId('bell-row')
        .filter({ hasText: `Уведомление клиенту «${name}»: доставка не удалась. Повторов не будет.` })).toBeVisible()
    }
    finally {
      if (clientId) await page.request.delete(`/api/clients/${clientId}`)
      await page.request.put('/api/admin/telegram', { data: { bot_token: '', chat_id: String(original.chat_id ?? '') } })
    }
  })
})
