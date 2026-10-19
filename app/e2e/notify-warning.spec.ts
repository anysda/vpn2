import type { Page, Route } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { addDevice, clientName, openClient, openHome } from './support/clients'
import { container, inside } from './support/openvpn-ca'

/**
 * Изменение записано, а уведомление в Telegram не встало в очередь бота:
 * сервер отвечает успехом с `warning`. Панель показывает этот текст в тосте
 * операции, а не обычный успех.
 */

const WARNING = 'Уведомление клиенту «e2e»: очередь заполнена. Повторов не будет.'

/** Настоящий ответ сервера с добавленным `warning`. */
async function withWarning(route: Route) {
  const response = await route.fetch()
  await route.fulfill({ response, json: { ...await response.json(), warning: WARNING } })
}

async function client(page: Page) {
  const name = uniq('warn')
  const created = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(created.status(), await created.text()).toBe(200)
  return { id: (await created.json()).id as number, name }
}

function warningToast(page: Page, title: string, text: string | RegExp) {
  return toast(page, title).filter({ hasText: text })
}

test('ответ с warning: отвязка Telegram, новый девайс, имя и лимит показывают предупреждение', async ({ page }) => {
  await openHome(page, '/clients')
  const { id, name } = await client(page)
  try {
    // Кнопка отзыва видна только у привязанного клиента.
    await page.route(new RegExp(`/api/clients/${id}$`), async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const response = await route.fetch()
      await route.fulfill({ response, json: { ...await response.json(), tgLinked: true } })
    })
    const panel = await openClient(page, name)

    // Обычный успех без warning - как раньше, без текста предупреждения.
    await addDevice(page, 'plain')
    await expect(toast(page, 'Девайс «plain» добавлен')).not.toContainText('Повторов не будет')

    await page.route(`**/api/clients/${id}/devices`, route => route.request().method() === 'POST' ? withWarning(route) : route.fallback())
    await addDevice(page, 'phone')
    await expect(warningToast(page, 'Девайс «phone» добавлен', WARNING)).toBeVisible()

    await page.route(new RegExp(`/api/clients/${id}$`), route => route.request().method() === 'PATCH' ? withWarning(route) : route.fallback())
    await clientName(panel).click()
    await clientName(panel).fill(`${name}-r`)
    await clientName(panel).press('Enter')
    await expect(warningToast(page, 'Имя сохранено', WARNING)).toBeVisible()
    // Кнопки +/- лимита молчат при успехе, но предупреждение не теряют.
    await panel.getByRole('button', { name: 'Безлимитно' }).click()
    await expect(warningToast(page, 'Лимит девайсов сохранён', WARNING)).toBeVisible()

    await page.route(`**/api/clients/${id}/unlink`, withWarning)
    await panel.getByRole('button', { name: 'Отозвать доступ к боту' }).click()
    await page.getByRole('dialog', { name: 'Отозвать доступ к боту?' }).getByRole('button', { name: 'Отозвать', exact: true }).click()
    await expect(warningToast(page, 'Доступ к боту отозван', WARNING)).toBeVisible()
    console.log(`Тост: Доступ к боту отозван · ${WARNING}`)
  }
  finally {
    await page.request.delete(`/api/clients/${id}`)
  }
})

test('ответ с warning: новый клиент показывает предупреждение', async ({ page }) => {
  await openHome(page, '/clients')
  const name = uniq('warn-new')
  await page.route(/\/api\/clients$/, route => route.request().method() === 'POST' ? withWarning(route) : route.fallback())
  await page.getByRole('button', { name: 'Новый клиент' }).click()
  await page.getByPlaceholder('Иван Иванов').fill(name)
  await page.getByRole('button', { name: 'Создать' }).click()
  await expect(warningToast(page, `Клиент «${name}» создан`, WARNING)).toBeVisible()
})

test('очередь бота заполнена: изменения записаны, панель предупреждает, ошибок нет', async ({ page }) => {
  test.skip(!container, 'нужен одноразовый локальный контейнер с подставным ботом')
  await openHome(page, '/clients')
  const original = await (await page.request.get('/api/admin/telegram')).json()
  test.skip(original.configured, 'настоящие настройки бота менять нельзя')
  const ids: number[] = []
  let held: string[] = []
  const link = (id: number) => inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    db.exec('PRAGMA busy_timeout=5000');
    db.prepare('UPDATE clients SET tg_chat_id = 123456789 WHERE id = ?').run(JSON.parse(process.argv[1]));
  `, id)
  try {
    const { id, name } = await client(page)
    ids.push(id)
    link(id)
    expect((await page.request.put('/api/admin/telegram', {
      data: { bot_token: '123456789:E2E_UNAVAILABLE_aaaaaaaaaaaaaaaaaaaaaaa', chat_id: '123456789' },
    })).ok()).toBe(true)
    // Очередь целиком из ожидающих: вытеснять нечего. Принятые ботом записи
    // прежних тестов освободили бы место - на время теста они вне счёта.
    // Срок повтора далеко, неудачных попыток нет - в «Уведомлениях» не видны.
    held = JSON.parse(inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec('PRAGMA busy_timeout=5000');
      const now = Date.now();
      const add = db.prepare("INSERT INTO bot_outbox (id, label, payload, status, attempts, last_failure, due, expires, created) VALUES (?, 'e2e', '{}', 'queued', 0, 0, ?, ?, ?)");
      db.exec('BEGIN');
      const held = db.prepare("SELECT id FROM bot_outbox WHERE status = 'accepted'").all().map(row => row.id);
      db.exec("UPDATE bot_outbox SET status = 'failed' WHERE status = 'accepted'");
      for (let i = 0; i < 1024; i++) add.run('e2e-full-' + i, now + 3600000, now + 3600000, now);
      db.exec('COMMIT');
      console.log(JSON.stringify(held));
    `))
    const full = new RegExp(`Уведомление клиенту «${name}»: очередь заполнена. Повторов не будет.`)

    const panel = await openClient(page, name)
    await addDevice(page, 'phone')
    await expect(warningToast(page, 'Девайс «phone» добавлен', full)).toBeVisible()
    await panel.getByRole('button', { name: 'Отозвать доступ к боту' }).click()
    await page.getByRole('dialog', { name: 'Отозвать доступ к боту?' }).getByRole('button', { name: 'Отозвать', exact: true }).click()
    await expect(warningToast(page, 'Доступ к боту отозван', full)).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Отозвать доступ к боту' })).toHaveCount(0)
    console.log(`Очередь заполнена, отвязка: ${(await toast(page, 'Доступ к боту отозван').textContent())?.trim()}`)

    link(id)
    const renamed = await page.request.patch(`/api/clients/${id}`, { data: { name: `${name}-r`, deviceLimit: 7 } })
    expect(renamed.status(), 'имя и лимит записаны, сбой уведомления - не ошибка запроса').toBe(200)
    const body = await renamed.json()
    expect(body).toMatchObject({ name: `${name}-r`, deviceLimit: 7 })
    expect(body.warning).toBe(`Уведомление клиенту «${name}-r»: очередь заполнена. Повторов не будет.`)

    const createdName = uniq('warn-full')
    const created = await page.request.post('/api/clients', { data: { name: createdName } })
    expect(created.status(), 'клиент создан, сбой уведомления - не ошибка запроса').toBe(200)
    const row = await created.json()
    ids.push(row.id)
    expect(row.warning).toBe(`Уведомление о клиенте «${createdName}»: очередь заполнена. Повторов не будет.`)
    console.log(`PATCH: ${body.warning} | POST: ${row.warning}`)
  }
  finally {
    inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec("PRAGMA busy_timeout=5000; DELETE FROM bot_outbox WHERE id LIKE 'e2e-full-%'");
      const back = db.prepare("UPDATE bot_outbox SET status = 'accepted' WHERE id = ?");
      for (const id of JSON.parse(process.argv[1])) back.run(id);
    `, held)
    for (const id of ids) await page.request.delete(`/api/clients/${id}`)
    await page.request.put('/api/admin/telegram', { data: { bot_token: '', chat_id: String(original.chat_id ?? '') } })
  }
})
