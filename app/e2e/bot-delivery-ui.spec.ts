import { test, expect, toast, uniq } from './support/fixtures'
import { openHome } from './support/clients'
import { container, inside } from './support/openvpn-ca'

test('недоступный бот: тост клиента, рассылки, восстановление и окончательная ошибка', async ({ page, request }) => {
  test.skip(!container, 'нужен одноразовый локальный контейнер с подставным ботом')
  const anonymous = await page.request.get('/api/admin/bot-deliveries')
  expect(anonymous.ok()).toBe(true)
  expect(await anonymous.json()).toEqual([])
  await openHome(page)
  const original = await (await page.request.get('/api/admin/telegram')).json()
  test.skip(original.configured, 'настоящие настройки бота менять нельзя')
  const name = uniq('delivery')
  let clientId: number | undefined
  const token = async (available: boolean) => {
    const response = await page.request.put('/api/admin/telegram', {
      data: { bot_token: available ? '123456789:E2E_ACCEPTED_aaaaaaaaaaaaaaaaaaaaaaaaaa' : '', chat_id: '123456789' },
    })
    expect(response.ok()).toBe(true)
  }
  const due = (id: string, exhausted = false) => inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    const result = db.prepare('UPDATE bot_outbox SET due = 0, attempts = ? WHERE id = ?').run(JSON.parse(process.argv[2]) ? 5 : 1, JSON.parse(process.argv[1]));
    if (result.changes !== 1) throw new Error('test delivery not found');
  `, id, exhausted)
  try {
    await token(false)
    const created = await page.request.post('/api/clients', { data: { name } })
    expect(created.ok()).toBe(true)
    clientId = (await created.json()).id
    await expect(toast(page, `Уведомление о клиенте «${name}»: не отправлено. Ещё попыток: 5.`)).toBeVisible()
    inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.prepare('UPDATE clients SET tg_chat_id = 123456789 WHERE id = ?').run(JSON.parse(process.argv[1]));
    `, clientId)
    const changed = await page.request.patch(`/api/clients/${clientId}`, { data: { deviceLimit: 4 } })
    expect(changed.ok()).toBe(true)
    await expect(toast(page, `Уведомление клиенту «${name}»: не отправлено. Ещё попыток: 5.`)).toBeVisible()

    await page.getByLabel('Сообщение юзерам').fill('e2e delivery probe')
    await page.getByRole('button', { name: 'Отправить', exact: true }).click()
    await expect(toast(page, 'Рассылка: не отправлено. Ещё попыток: 5.')).toBeVisible()
    await expect(toast(page, /^Отправлено:/)).toHaveCount(0)
    const statuses = await (await page.request.get('/api/admin/bot-deliveries')).json()
    expect(await (await request.get('/api/admin/bot-deliveries')).json(), 'без сессии история недоступна').toEqual([])
    const broadcast = statuses.find((row: { label: string }) => row.label === 'Рассылка')
    expect(broadcast).toMatchObject({ status: 'queued', remaining: 5 })
    await token(true)
    due(broadcast.id)
    try {
      await expect.poll(async () => (await (await page.request.get('/api/admin/bot-deliveries')).json())
        .find((row: { id: string }) => row.id === broadcast.id)).toMatchObject({ status: 'accepted' })
    }
    catch (error) {
      console.log(inside(`
        import { DatabaseSync } from 'node:sqlite';
        const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
        console.log(JSON.stringify(db.prepare('SELECT id, status, attempts, due FROM bot_outbox').all()));
        console.log('bot health', (await fetch('http://127.0.0.1:8877/health')).status);
      `))
      throw error
    }
    const accepted = JSON.parse(inside(`console.log(JSON.stringify(await (await fetch('http://127.0.0.1:8877/events')).json()))`))
    expect(accepted.filter((row: { deliveryId: string }) => row.deliveryId === broadcast.id)).toHaveLength(1)

    await token(false)
    const pending = statuses.find((row: { label: string }) => row.label === `Уведомление клиенту «${name}»`)
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
