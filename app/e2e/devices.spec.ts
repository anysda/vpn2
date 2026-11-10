import type { Locator, Page, Response, APIRequestContext } from '@playwright/test'
import { downloadText, expect, PUBLIC_HOST, toast, uniq, loginApi, test } from './support/fixtures'
import { addDevice, addDeviceButton, configDialog, createClientUi, deviceCard, ikeField, openClient, openHome } from './support/clients'
import { container, inside, fault, restore, rows } from './support/openvpn-ca'
import { proxiesBarrier as proxies } from './support/proxies-barrier'
import { randomUUID, X509Certificate } from 'node:crypto'
import { waitAccess } from './support/access'

test.describe('devices', () => {
  function closeBtn(dlg: Locator) {
    return dlg.getByRole('button', { name: /Close|Закрыть/ })
  }

  async function download(page: Page, dlg: Locator, name = 'Скачать') {
    const wait = page.waitForEvent('download')
    await dlg.getByRole('button', { name, exact: true }).click()
    const d = await wait
    return { file: d.suggestedFilename(), text: await downloadText(d) }
  }

  function field(conf: string, key: string): string | undefined {
    return new RegExp(`^${key}\\s*=\\s*(.+)$`, 'm').exec(conf)?.[1]?.trim()
  }

  function block(conf: string, tag: string): string | undefined {
    return new RegExp(`<${tag}>\\n([\\s\\S]+?)\\n</${tag}>`).exec(conf)?.[1]
  }

  /** Все ответы QR-картинки WireGuard с телами — для проверки «QR перезапросился». */
  function qrResponses(page: Page) {
    const got: { url: string, body: string }[] = []
    page.on('response', async (r: Response) => {
      if (!/\/wg-qrcode\.svg/.test(r.url()) || r.status() !== 200) return
      got.push({ url: r.url(), body: await r.text().catch(() => '') })
    })
    return got
  }

  async function qrLoaded(dlg: Locator) {
    const img = dlg.locator('img[alt="WireGuard QR"]')
    await expect(img).toBeVisible()
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0)
  }

  test.describe('девайсы', () => {
    test('WG: QR, .conf, перевыпуск меняет ключ и QR, удаление', async ({ page }) => {
      const qrs = qrResponses(page)
      await openHome(page, '/clients')
      const client = uniq('wg')
      const device = 'e2e-phone'
      await createClientUi(page, client)
      await openClient(page, client)

      // «Отмена» в «Новый девайс».
      await addDeviceButton(page).click()
      const add = page.getByRole('dialog', { name: 'Новый девайс' })
      await add.getByPlaceholder('напр. iPhone').fill('lost')
      await add.getByRole('button', { name: 'Отмена' }).click()
      await expect(add).toBeHidden()
      await expect(page.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()

      await addDevice(page, device)
      const dc = deviceCard(page, device)

      await dc.wg.click()
      const wg = configDialog(page, device, 'WireGuard')
      await qrLoaded(wg)
      const first = await download(page, wg)
      expect(first.file).toMatch(/\.conf$/)
      expect(first.text).toContain('[Interface]')
      expect(first.text).toContain('[Peer]')
      const key1 = field(first.text, 'PrivateKey')
      expect(key1).toMatch(/^[A-Za-z0-9+/]{43}=$/)
      expect(field(first.text, 'Address')).toMatch(/^\d+\.\d+\.\d+\.\d+\/\d+/)
      expect(field(first.text, 'PublicKey')).toMatch(/^[A-Za-z0-9+/]{43}=$/)
      expect(field(first.text, 'Endpoint')).toMatch(new RegExp(`^${PUBLIC_HOST.replace(/\./g, '\\.')}:\\d+$`))
      expect(qrs.length).toBeGreaterThan(0)
      const qr1 = qrs.at(-1)!.body
      expect(qr1).toContain('<svg')
      await closeBtn(wg).click()
      await expect(wg).toBeHidden()

      // Перевыпуск: «Отмена» не трогает ключ, «Перевыпустить» меняет.
      await dc.reissue.click()
      const confirm = page.getByRole('dialog', { name: 'Перевыпустить ключи девайса?' })
      await confirm.getByRole('button', { name: 'Отмена' }).click()
      await expect(confirm).toBeHidden()
      await dc.wg.click()
      expect(field((await download(page, wg)).text, 'PrivateKey')).toBe(key1)
      await closeBtn(wg).click()

      await dc.reissue.click()
      await confirm.getByRole('button', { name: 'Перевыпустить' }).click()
      await expect(toast(page, 'Перевыпуск выполняется')).toBeVisible()
      await expect(page.getByTestId('client-detail').getByRole('status')).toContainText('Ключи перевыпущены', { timeout: 45_000 })
      await expect(confirm).toBeHidden()

      const before = qrs.length
      await dc.wg.click()
      await qrLoaded(wg)
      const second = await download(page, wg)
      const key2 = field(second.text, 'PrivateKey')
      expect(key2).toMatch(/^[A-Za-z0-9+/]{43}=$/)
      expect(key2).not.toBe(key1)
      // QR в модалке должен показывать новый конфиг, а не картинку из кэша.
      await expect.poll(() => qrs.length, { message: 'QR не перезапрошен после перевыпуска' }).toBeGreaterThan(before)
      expect(qrs.at(-1)!.body).not.toBe(qr1)
      await page.keyboard.press('Escape')
      await expect(wg).toBeHidden()

      // Удаление: «Отмена», потом всерьёз.
      await dc.remove.click()
      const del = page.getByRole('dialog', { name: 'Удалить девайс?' })
      await del.getByRole('button', { name: 'Отмена' }).click()
      await expect(del).toBeHidden()
      await expect(dc.card).toBeVisible()
      await dc.remove.click()
      await del.getByRole('button', { name: 'Удалить' }).click()
      await expect(toast(page, 'Отключение выполняется')).toBeVisible()
      await expect(dc.card).toHaveCount(0, { timeout: 45_000 })
      await expect(page.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()
    })

    test('OpenVPN: .ovpn с сертификатами, перевыпуск меняет сертификат', async ({ page }) => {
      await openHome(page, '/clients')
      const client = uniq('ovpn')
      const device = 'e2e-laptop'
      await createClientUi(page, client)
      await openClient(page, client)
      await addDevice(page, device)
      const dc = deviceCard(page, device)

      await dc.ovpn.click()
      const ovpn = configDialog(page, device, 'OpenVPN')
      await expect(ovpn.getByText('генерирую сертификат…')).toBeHidden({ timeout: 30_000 })
      const first = await download(page, ovpn)
      expect(first.file).toMatch(/\.ovpn$/)
      expect(first.text).toMatch(new RegExp(`^remote ${PUBLIC_HOST.replace(/\./g, '\\.')} \\d+$`, 'm'))
      for (const tag of ['ca', 'cert', 'key']) expect(block(first.text, tag), tag).toMatch(/^-----BEGIN [A-Z ]+-----/)
      expect(block(first.text, 'tls-crypt')).toContain('BEGIN OpenVPN Static key V1')
      const cert1 = block(first.text, 'cert')
      await closeBtn(ovpn).click()
      await expect(ovpn).toBeHidden()

      await dc.reissue.click()
      await page.getByRole('dialog', { name: 'Перевыпустить ключи девайса?' }).getByRole('button', { name: 'Перевыпустить' }).click()
      await expect(toast(page, 'Перевыпуск выполняется')).toBeVisible()
      await expect(page.getByTestId('client-detail').getByRole('status')).toContainText('Ключи перевыпущены', { timeout: 45_000 })

      await dc.ovpn.click()
      await expect(ovpn.getByText('генерирую сертификат…')).toBeHidden({ timeout: 30_000 })
      const second = await download(page, ovpn)
      expect(block(second.text, 'cert')).toMatch(/^-----BEGIN CERTIFICATE-----/)
      expect(block(second.text, 'cert')).not.toBe(cert1)
      await closeBtn(ovpn).click()
      await expect(ovpn).toBeHidden()
    })

    test('IKEv2: сервер и логин, пароль скрыт и показывается, CA скачивается', async ({ page }) => {
      await openHome(page, '/clients')
      const client = uniq('ikev2')
      const device = 'e2e-mac'
      await createClientUi(page, client)
      await openClient(page, client)
      await addDevice(page, device)
      const dc = deviceCard(page, device)

      await dc.ikev2.click()
      const ike = configDialog(page, device, 'IKEv2')
      const password = ikeField(ike, 'Пароль')
      await expect(password).toBeVisible()
      await expect(ikeField(ike, 'Сервер')).toHaveText(PUBLIC_HOST)
      await expect(ikeField(ike, 'Логин')).not.toBeEmpty()
      await expect(password).toHaveText('•'.repeat(20))

      const eye = ike.getByRole('button', { name: 'Показать' })
      await eye.click()
      const shown = (await password.textContent())!.trim()
      expect(shown).not.toContain('•')
      expect(shown.length).toBeGreaterThanOrEqual(12)
      await eye.click()
      await expect(password).toHaveText('•'.repeat(20))

      // Self-signed режим: есть «Скачать CA», файл — PEM-сертификат.
      const ca = ike.getByRole('link', { name: 'Скачать CA' }).or(ike.getByRole('button', { name: 'Скачать CA' }))
      await expect(ca).toBeVisible()
      const href = await ca.getAttribute('href')
      expect(href).toBe('/api/ikev2/ca.crt')
      const r = await page.request.get(href!)
      expect(r.status()).toBe(200)
      expect(await r.text()).toContain('-----BEGIN CERTIFICATE-----')

      await closeBtn(ike).click()
      await expect(ike).toBeHidden()
    })
  })
})

test.describe('device-submit', () => {
  for (const width of [1400, 390]) {
    test.describe(`отправка девайса, ${width}px`, () => {
      test.use({ viewport: { width, height: 844 } })

      test('повторный Enter во время запроса не отправляет второй POST', async ({ page, guard }) => {
        await openHome(page, '/clients')
        const name = uniq('submit')
        await createClientUi(page, name)
        await openClient(page, name)
        await addDeviceButton(page).click()
        const dialog = page.getByRole('dialog', { name: 'Новый девайс' })
        const input = dialog.getByPlaceholder('напр. iPhone')
        await input.fill('e2e-phone')

        let posts = 0
        let release!: () => void
        const held = new Promise<void>((resolve) => { release = resolve })
        let created!: () => void
        const ready = new Promise<void>((resolve) => { created = resolve })
        guard.allow(409, /\/devices$/, 'POST')
        await page.route('**/api/clients/*/devices', async (route) => {
          if (route.request().method() !== 'POST') return route.continue()
          posts++
          const response = await route.fetch()
          created()
          await held
          await route.fulfill({ response })
        })
        try {
          await input.press('Enter')
          await ready
          await input.press('Enter')
        }
        finally {
          release()
        }
        await expect(toast(page, 'Девайс «e2e-phone» добавлен')).toBeVisible()
        await expect(dialog).toBeHidden()
        await page.unrouteAll({ behavior: 'wait' })
        expect(posts, 'одна операция пользователя должна отправить один POST').toBe(1)
      })
    })
  }
})

test.describe('device-limit-race', () => {
  test('лимит проверяется при записи после параллельного создания устройства', async ({ page }) => {
    test.skip(!container, 'барьер sing-box доступен только в одноразовом e2e-контейнере')
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('limit-race'), deviceLimit: 3 } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    const url = `/api/clients/${client.id}`
    try {
      expect((await page.request.post(`${url}/devices`, { data: { name: 'first' } })).status()).toBe(200)
      proxies('POST', { device: { clientId: client.id, name: 'second' } })
      const result = await page.request.patch(url, { data: { deviceLimit: 1, preferredExit: 'nl' } })
      expect(proxies('GET').insertedId, 'конкурентная запись выполнена после первой проверки лимита').not.toBeNull()
      const after = await (await page.request.get(url)).json()
      console.log(`limit race: PATCH=${result.status()}, limit=${after.deviceLimit}, devices=${after.devices.length}`)
      expect(result.status()).toBe(409)
      expect(await result.json()).toMatchObject({ statusMessage: 'device_limit_below_count' })
      expect(after.deviceLimit).toBe(3)
      expect(after.preferredExit).toBeNull()
      expect(after.devices).toHaveLength(2)
    }
    finally {
      proxies('DELETE')
      await page.request.delete(url)
    }
  })
})

test.describe('device-idempotency', () => {
  /**
   * Создание устройства принимает Idempotency-Key, как операции доступа:
   * повтор после обрыва с тем же ключом возвращает то же устройство, тот же
   * ключ с другим телом отклоняется. Без ключа повтор упирается в имя (409).
   */

  async function newClient(api: APIRequestContext) {
    const r = await api.post('/api/clients', { data: { name: uniq('idem'), deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    return (await r.json()).id as number
  }

  async function devices(api: APIRequestContext, clientId: number) {
    const r = await api.get(`/api/clients/${clientId}`)
    expect(r.status(), await r.text()).toBe(200)
    return (await r.json()).devices as { id: number, name: string }[]
  }

  function addDevice(api: APIRequestContext, clientId: number, name: string, key?: string) {
    return api.post(`/api/clients/${clientId}/devices`, {
      data: { name },
      headers: key ? { 'idempotency-key': key } : {},
    })
  }

  test.describe('ключ идемпотентности при создании устройства', () => {
    let clientId: number

    test.beforeEach(async ({ page }) => {
      await loginApi(page)
      clientId = await newClient(page.request)
    })

    test.afterEach(async ({ page }) => {
      await page.request.delete(`/api/clients/${clientId}`)
    })

    test('панель: повтор с тем же ключом и телом возвращает то же устройство', async ({ page }) => {
      const key = randomUUID()
      const first = await addDevice(page.request, clientId, 'phone', key)
      expect(first.status(), await first.text()).toBe(200)
      const second = await addDevice(page.request, clientId, 'phone', key)
      expect(second.status(), await second.text()).toBe(200)
      expect((await second.json()).id).toBe((await first.json()).id)
      expect((await devices(page.request, clientId)).filter(d => d.name === 'phone')).toHaveLength(1)
    })

    test('панель: тот же ключ с другим именем отклоняется', async ({ page }) => {
      const key = randomUUID()
      const first = await addDevice(page.request, clientId, 'phone', key)
      expect(first.status(), await first.text()).toBe(200)
      const other = await addDevice(page.request, clientId, 'laptop', key)
      expect(other.status(), await other.text()).toBeGreaterThanOrEqual(400)
      expect(other.status()).toBeLessThan(500)
      expect((await devices(page.request, clientId)).map(d => d.name)).toEqual(['phone'])
    })

    test('панель: без ключа повтор с тем же именем по-прежнему 409', async ({ page }) => {
      const first = await addDevice(page.request, clientId, 'phone')
      expect(first.status(), await first.text()).toBe(200)
      const again = await addDevice(page.request, clientId, 'phone')
      expect(again.status(), await again.text()).toBe(409)
      expect((await again.json()).statusMessage).toBe('device_exists')
      expect(await devices(page.request, clientId)).toHaveLength(1)
    })

    test('бот: повтор с тем же ключом и телом возвращает то же устройство', async ({ page }) => {
      test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
      const chatId = 800000 + clientId
      inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
      const [id, chat] = process.argv.slice(1).map(JSON.parse);
      db.prepare('UPDATE clients SET tg_chat_id=? WHERE id=?').run(chat, id);
    `, clientId, chatId)
      const key = randomUUID()
      const post = () => JSON.parse(inside(`
      const [chatId, key] = process.argv.slice(1).map(JSON.parse);
      const r = await fetch('http://127.0.0.1:31821/api/bot/devices', {
        method: 'POST',
        headers: { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET, 'content-type': 'application/json', 'idempotency-key': key },
        body: JSON.stringify({ chatId, name: 'phone' }),
      });
      console.log(JSON.stringify({ status: r.status, body: await r.json() }));
    `, chatId, key)) as { status: number, body: { devices?: { id: number, name: string }[] } }
      const first = post()
      expect(first.status, JSON.stringify(first.body)).toBe(200)
      const second = post()
      expect(second.status, JSON.stringify(second.body)).toBe(200)
      const phones = (body: typeof first.body) => (body.devices ?? []).filter(d => d.name === 'phone').map(d => d.id)
      expect(phones(first.body)).toHaveLength(1)
      expect(phones(second.body)).toEqual(phones(first.body))
      expect((await devices(page.request, clientId)).filter(d => d.name === 'phone')).toHaveLength(1)
    })
  })
})

test.describe('device-idempotency-edge', () => {
  /**
   * Края ключа идемпотентности при создании устройства: ключ переживает
   * удаление устройства, а повтор из бота не отдаёт устройство прежнего
   * владельца чата после перепривязки.
   */

  async function newClient(api: APIRequestContext) {
    const r = await api.post('/api/clients', { data: { name: uniq('idem'), deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    return (await r.json()).id as number
  }

  async function devices(api: APIRequestContext, clientId: number) {
    const r = await api.get(`/api/clients/${clientId}`)
    expect(r.status(), await r.text()).toBe(200)
    return (await r.json()).devices as { id: number, name: string }[]
  }

  type BotReply = { status: number, body: { devices?: { id: number, name: string }[], statusMessage?: string } }

  function bot(path: string, payload: unknown, key?: string): BotReply {
    return JSON.parse(inside(`
    const [path, payload, key] = process.argv.slice(1).map(JSON.parse);
    const headers = { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET, 'content-type': 'application/json' };
    if (key) headers['idempotency-key'] = key;
    const r = await fetch('http://127.0.0.1:31821' + path, { method: 'POST', headers, body: JSON.stringify(payload) });
    console.log(JSON.stringify({ status: r.status, body: await r.json() }));
  `, path, payload, key ?? null)) as BotReply
  }

  // Инвайт клиента — ?start=<password>; панель отдаёт пароль только в Telegram.
  function linkChat(clientId: number, chatId: number) {
    const password = inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
    console.log(db.prepare('SELECT password FROM clients WHERE id=?').get(JSON.parse(process.argv[1])).password);
  `, clientId)
    const r = bot('/api/bot/link', { password, chatId })
    expect(r.status, JSON.stringify(r.body)).toBe(200)
  }

  test.describe('ключ идемпотентности создания устройства: края', () => {
    const created: number[] = []

    test.beforeEach(async ({ page }) => {
      await loginApi(page)
    })

    test.afterEach(async ({ page }) => {
      for (const id of created.splice(0)) await page.request.delete(`/api/clients/${id}`)
    })

    test('панель: повтор ключа после удаления устройства не создаёт его заново', async ({ page }) => {
      const clientId = await newClient(page.request)
      created.push(clientId)
      const key = randomUUID()
      const add = () => page.request.post(`/api/clients/${clientId}/devices`, {
        data: { name: 'phone' },
        headers: { 'idempotency-key': key },
      })

      const first = await add()
      expect(first.status(), await first.text()).toBe(200)
      const deviceId = (await first.json()).id as number

      const removal = await page.request.delete(`/api/clients/${clientId}/devices/${deviceId}`)
      expect(removal.status(), await removal.text()).toBe(202)
      await waitAccess(page.request, (await removal.json()).operation)
      await expect.poll(async () => (await devices(page.request, clientId)).map(d => d.id)).not.toContain(deviceId)

      const again = await add()
      expect(again.status(), await again.text()).toBeGreaterThanOrEqual(400)
      expect(again.status()).toBeLessThan(500)
      expect(await devices(page.request, clientId)).toHaveLength(0)
    })

    test('бот: повтор ключа после перепривязки чата не отдаёт устройство прежнего клиента', async ({ page }) => {
      test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
      const a = await newClient(page.request)
      created.push(a)
      const b = await newClient(page.request)
      created.push(b)
      const chatId = 900000 + a
      const key = randomUUID()

      linkChat(a, chatId)
      const first = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
      expect(first.status, JSON.stringify(first.body)).toBe(200)
      const phoneOfA = (await devices(page.request, a)).find(d => d.name === 'phone')
      expect(phoneOfA).toBeDefined()

      // Панель: «Отозвать доступ к боту» у A, затем чат входит по инвайту B.
      const unlink = await page.request.post(`/api/clients/${a}/unlink`)
      expect(unlink.status(), await unlink.text()).toBe(200)
      linkChat(b, chatId)

      const again = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
      expect(again.status, JSON.stringify(again.body)).toBeGreaterThanOrEqual(400)
      expect(again.status).toBeLessThan(500)
      expect((await devices(page.request, b)).map(d => d.id)).not.toContain(phoneOfA!.id)
    })

    test('панель: повтор ключа после удаления клиента — 410 device_deleted', async ({ page }) => {
      const clientId = await newClient(page.request)
      created.push(clientId)
      const key = randomUUID()
      const add = () => page.request.post(`/api/clients/${clientId}/devices`, {
        data: { name: 'phone' },
        headers: { 'idempotency-key': key },
      })

      const first = await add()
      expect(first.status(), await first.text()).toBe(200)
      await removeClient(page.request, clientId)
      const before = await everyone(page.request)

      const again = await add()
      expect(again.status(), await again.text()).toBe(410)
      expect((await again.json()).statusMessage).toBe('device_deleted')
      expect(await everyone(page.request)).toEqual(before)
      expect((await page.request.get(`/api/clients/${clientId}`)).status()).toBe(404)
    })

    test('бот: повтор ключа после удаления клиента — 410 device_deleted', async ({ page }) => {
      test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
      const clientId = await newClient(page.request)
      created.push(clientId)
      const chatId = 910000 + clientId
      const key = randomUUID()

      linkChat(clientId, chatId)
      const first = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
      expect(first.status, JSON.stringify(first.body)).toBe(200)
      await removeClient(page.request, clientId)
      const before = await everyone(page.request)

      const again = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
      expect(again.status, JSON.stringify(again.body)).toBe(410)
      expect(again.body.statusMessage).toBe('device_deleted')
      expect(await everyone(page.request)).toEqual(before)
    })

    test('бот: повтор ключа из отвязанного чата при живом клиенте — 404 not_linked без устройства', async ({ page }) => {
      test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
      const clientId = await newClient(page.request)
      created.push(clientId)
      const chatId = 920000 + clientId
      const key = randomUUID()
      const name = uniq('phone')

      linkChat(clientId, chatId)
      const first = bot('/api/bot/devices', { chatId, name }, key)
      expect(first.status, JSON.stringify(first.body)).toBe(200)
      const [phone] = await devices(page.request, clientId)
      expect(phone?.name).toBe(name)
      const unlink = await page.request.post(`/api/clients/${clientId}/unlink`)
      expect(unlink.status(), await unlink.text()).toBe(200)

      const again = bot('/api/bot/devices', { chatId, name }, key)
      expect(again.status, JSON.stringify(again.body)).toBe(404)
      expect(again.body.statusMessage).toBe('not_linked')
      expect(again.body.devices).toBeUndefined()
      expect(JSON.stringify(again.body)).not.toContain(name)
      expect((await devices(page.request, clientId)).map(d => d.id)).toEqual([phone!.id])
    })

    test('панель: незнакомый ключ для удалённого клиента — 404 not_found', async ({ page }) => {
      const clientId = await newClient(page.request)
      created.push(clientId)
      await removeClient(page.request, clientId)
      const before = await everyone(page.request)

      const r = await page.request.post(`/api/clients/${clientId}/devices`, {
        data: { name: 'phone' },
        headers: { 'idempotency-key': randomUUID() },
      })
      expect(r.status(), await r.text()).toBe(404)
      expect((await r.json()).statusMessage).toBe('not_found')
      expect(await everyone(page.request)).toEqual(before)
    })

    test('повтор ключа, пока клиент удаляется, — 410 device_deleted в панели и в боте', async ({ page }) => {
      test.setTimeout(180_000)
      test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере')
      const clientId = await newClient(page.request)
      created.push(clientId)
      const chatId = 930000 + clientId
      const panelKey = randomUUID()
      const botKey = randomUUID()
      const panelAdd = () => page.request.post(`/api/clients/${clientId}/devices`, {
        data: { name: 'laptop' },
        headers: { 'idempotency-key': panelKey },
      })

      linkChat(clientId, chatId)
      expect((await panelAdd()).status()).toBe(200)
      const fromBot = bot('/api/bot/devices', { chatId, name: 'phone' }, botKey)
      expect(fromBot.status, JSON.stringify(fromBot.body)).toBe(200)
      const saved = rows(clientId)
      expect(saved).toHaveLength(2)
      try {
        // Отзыв OpenVPN не проходит: клиент остаётся в состоянии «удаляется».
        fault('revoke', new X509Certificate(saved[0]!.ovpn_cert).serialNumber)
        const removal = await page.request.delete(`/api/clients/${clientId}`)
        expect(removal.status(), await removal.text()).toBe(202)
        const { operation } = await removal.json()
        await expect.poll(async () => (await (await page.request.get(`/api/access-operations/${operation.id}`)).json()).protocols.openvpn.state, { timeout: 30_000 }).toBe('failed')
        const detail = await page.request.get(`/api/clients/${clientId}`)
        expect(detail.status(), await detail.text()).toBe(200)
        expect((await detail.json()).deleting).toBe(true)

        const panelAgain = await panelAdd()
        expect(panelAgain.status(), await panelAgain.text()).toBe(410)
        expect((await panelAgain.json()).statusMessage).toBe('device_deleted')
        const botAgain = bot('/api/bot/devices', { chatId, name: 'phone' }, botKey)
        expect(botAgain.status, JSON.stringify(botAgain.body)).toBe(410)
        expect(botAgain.body.statusMessage).toBe('device_deleted')
        expect(rows(clientId).map(d => d.id)).toEqual(saved.map(d => d.id))

        restore()
        await page.request.post(`/api/access-operations/${operation.id}/retry`)
        await waitAccess(page.request, operation)
      }
      finally {
        restore()
      }
    })
  })

  async function removeClient(api: APIRequestContext, clientId: number) {
    const removal = await api.delete(`/api/clients/${clientId}`)
    expect(removal.status(), await removal.text()).toBe(202)
    await waitAccess(api, (await removal.json()).operation)
    await expect.poll(async () => (await api.get(`/api/clients/${clientId}`)).status()).toBe(404)
  }

  // Все клиенты и число их устройств: повтор не должен ничего создать.
  async function everyone(api: APIRequestContext) {
    const r = await api.get('/api/clients')
    expect(r.status(), await r.text()).toBe(200)
    return ((await r.json()) as { id: number, deviceCount: number }[])
      .map(c => [c.id, c.deviceCount]).sort((a, b) => a[0]! - b[0]!)
  }
})
