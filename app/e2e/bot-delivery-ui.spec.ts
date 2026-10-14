import { test, expect, toast, uniq } from './support/fixtures'
import { openHome } from './support/clients'
import { container, inside } from './support/openvpn-ca'
import { checkBotQueue } from './support/bot-queue'

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
    await page.route('**/api/admin/bot-deliveries', route => route.fulfill({ json: [
      { id: 'yesterday', label: 'Рассылка', status: 'failed', remaining: 0, created: Date.parse('2026-10-13T20:30:00Z') },
      { id: 'after-midnight', label: 'Уведомление клиенту «Ночь»', status: 'failed', remaining: 0, created: Date.parse('2026-10-13T21:05:00Z') },
      { id: 'today', label: 'Уведомление клиенту «День»', status: 'queued', remaining: 2, created: Date.parse('2026-10-14T11:31:00Z') },
    ] }))
    await openHome(page)
    await page.getByRole('button', { name: 'Уведомления: 3', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Доставка уведомлений' })
    const row = (text: string) => dialog.locator('.bi').filter({ hasText: text })
    await expect(row('Рассылка: доставка не удалась').locator('small')).toHaveText('вчера 23:30')
    await expect(row('«Ночь»').locator('small')).toHaveText('сегодня 00:05')
    await expect(row('«День»: не отправлено. Ещё попыток: 2.').locator('small')).toHaveText('сегодня 14:31')
    console.log('Колокольчик:', (await dialog.locator('.bi small').allTextContents()).join(' | '))
  })
})

test('недоступный бот: тост клиента, рассылки, восстановление и окончательная ошибка', async ({ page, request }) => {
  test.skip(!container, 'нужен одноразовый локальный контейнер с подставным ботом')
  const anonymous = await page.request.get('/api/admin/bot-deliveries')
  expect(anonymous.ok()).toBe(true)
  expect(await anonymous.json()).toEqual([])
  await openHome(page, '/settings')
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
    await expect(page.getByRole('status').filter({ hasText: 'Бот недоступен, 1 уведомление в очереди' })).toBeVisible()
    await page.getByRole('button', { name: 'Уведомления: 1', exact: true }).click()
    await expect(page.getByRole('dialog').getByText('Бот недоступен, 1 уведомление в очереди', { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    console.log('Реальная очередь: Бот недоступен, 1 уведомление в очереди — тост, «Боты», «Уведомления»')
    inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec('PRAGMA busy_timeout=5000');
      db.prepare('UPDATE clients SET tg_chat_id = 123456789 WHERE id = ?').run(JSON.parse(process.argv[1]));
    `, clientId)
    const changed = await page.request.patch(`/api/clients/${clientId}`, { data: { deviceLimit: 4 } })
    expect(changed.ok()).toBe(true)
    await expect(page.getByRole('button', { name: 'Уведомления: 2', exact: true })).toBeVisible()
    await expect(page.getByRole('status').filter({ hasText: 'Бот недоступен, 2 уведомления в очереди' })).toBeVisible()
    await page.getByRole('button', { name: 'Уведомления: 2', exact: true }).click()
    await expect(page.getByRole('dialog').getByText('Бот недоступен, 2 уведомления в очереди', { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    console.log('Реальная очередь: Бот недоступен, 2 уведомления в очереди — «Боты», «Уведомления»')

    for (let n = 3; n <= 5; n++) {
      await page.getByLabel('Сообщение юзерам').fill(`e2e delivery probe ${n}`)
      await page.getByRole('button', { name: 'Отправить', exact: true }).click()
      await expect(page.getByRole('button', { name: `Уведомления: ${n}`, exact: true })).toBeVisible()
    }
    const queueToasts = page.getByRole('region', { includeHidden: true }).locator('ol > li').filter({ hasText: /Бот недоступен|не отправлено/ })
    await page.mouse.move(0, 0)
    await expect(queueToasts, 'реальная очередь остаётся, короткий тост гаснет').toHaveCount(0)
    await expect(page.getByRole('status').filter({ hasText: 'Бот недоступен, 5 уведомлений в очереди' })).toBeVisible()
    await page.getByRole('button', { name: 'Уведомления: 5', exact: true }).click()
    await expect(page.getByRole('dialog').getByText('Бот недоступен, 5 уведомлений в очереди', { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    console.log('Реальная очередь: Бот недоступен, 5 уведомлений в очереди — в «Уведомлениях»; тостов: 0')
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
    await expect(page.getByText(`Уведомление клиенту «${name}»: доставка не удалась. Повторов не будет.`, { exact: true }).last()).toBeVisible()
  }
  finally {
    if (clientId) await page.request.delete(`/api/clients/${clientId}`)
    await page.request.put('/api/admin/telegram', { data: { bot_token: '', chat_id: String(original.chat_id ?? '') } })
  }
})
