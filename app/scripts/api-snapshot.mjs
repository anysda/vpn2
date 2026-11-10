// @ts-check
// Эталон ответов API панели: обход маршрутов bot/, admin/, clients/ и
// ops/bot-snapshot на e2e-стенде с подставными бэкендами. Сценарий с фиксированными
// именами и порядком вызовов пишет на каждый вызов метод, путь-шаблон, статус,
// content-type и тело. Шаг 2 рефактора панели обязан доказать «байт в байт»:
// спек e2e/api-snapshot.spec.ts сравнивает строку файла с эталоном.
//
// Нормализация только по таблице NORMALIZATION: маршрут, поле, чем заменяется,
// причина. Поле вне таблицы сравнивается как есть; масок по виду значения нет.

const CLIENT_PREFIX = 'e2e-api-snapshot-'
const TG_TOKEN = '123456789:E2E_ACCEPTED_apisnapshot_aaaaaaaaaaaaaaaa'
const CHAT_ID = 777000149
const MISSING_ID = 2147483000
const MISSING_OPERATION = '00000000-0000-4000-8000-000000000000'

/**
 * @typedef {{ status: number, type: string, text: string }} Reply
 * @typedef {{
 *   admin: (method: string, path: string, data?: unknown) => Promise<Reply>,
 *   bot: (method: string, path: string, data?: unknown, auth?: boolean) => Promise<Reply>,
 *   settled: (operationId: string) => Promise<void>,
 *   gone: (path: string) => Promise<void>,
 * }} Io
 * @typedef {{ method: string, path: string, status: number, contentType: string, body: unknown }} Call
 */

/**
 * Что заменяется и почему. kind: field - поле JSON по пути (* - каждый элемент
 * массива), line - значение строки `Ключ = значение` текстового тела, block -
 * содержимое блока <тег>…</тег>, body - всё тело. check - формат исходного
 * значения: он проверяется отдельно и валит прогон, если не совпал.
 * @type {{ route: string, kind: 'field' | 'line' | 'block' | 'body', key: string, label: string, check: RegExp, reason: string, status?: number }[]}
 */
const NORMALIZATION = []

/**
 * @param {string} route
 * @param {string} method
 * @param {string} path
 * @param {Reply} reply
 * @returns {Call}
 */
function toCall(route, method, path, reply) {
  void route
  /** @type {unknown} */
  let body = reply.text
  if (/json/.test(reply.type)) {
    try { body = JSON.parse(reply.text) }
    catch { body = reply.text }
  }
  return { method, path, status: reply.status, contentType: reply.type, body }
}

/**
 * Обход. Возвращает сырые вызовы по маршрутам в порядке выполнения.
 * @param {Io} io
 * @returns {Promise<Map<string, Call[]>>}
 */
export async function walk(io) {
  /** @type {Map<string, Call[]>} */
  const calls = new Map()
  /** @type {Map<string, any>} */
  const raw = new Map()
  const vars = /** @type {Record<string, string | number>} */ ({})

  /**
   * @param {string} route
   * @param {'admin' | 'bot'} side
   * @param {string} method
   * @param {string} template путь с метками {имя}; метки берутся из vars
   * @param {unknown} [data]
   * @param {boolean} [auth]
   */
  async function call(route, side, method, template, data, auth = true) {
    const path = template.replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? name))
    const reply = side === 'admin' ? await io.admin(method, path, data) : await io.bot(method, path, data, auth)
    const entry = toCall(route, method, template, reply)
    calls.set(route, [...calls.get(route) ?? [], entry])
    return { reply, json: /** @type {any} */ (entry.body) }
  }
  const adminCall = (/** @type {string} */ route, /** @type {string} */ method, /** @type {string} */ template, /** @type {unknown} */ data) =>
    call(route, 'admin', method, template, data)
  const botCall = (/** @type {string} */ route, /** @type {string} */ method, /** @type {string} */ template, /** @type {unknown} */ data, /** @type {boolean} */ auth = true) =>
    call(route, 'bot', method, template, data, auth)

  /** @param {any} json */
  const settle = async (json) => {
    const id = json?.operation?.id
    if (typeof id === 'string') await io.settled(id)
  }

  // Telegram: подставной бот принимает токен с E2E_ACCEPTED.
  await adminCall('admin/telegram.put', 'PUT', '/api/admin/telegram', { bot_token: TG_TOKEN, chat_id: '123456789', admin_username: '@snapshot_admin' })
  await adminCall('admin/telegram.get', 'GET', '/api/admin/telegram')

  // SSO без включения: сохраняется известное состояние, затем читается.
  const sso = { enabled: false, authentikUrl: 'https://auth.example.invalid', appSlug: 'vpn2-snapshot', clientId: 'vpn2-snapshot', clientSecret: 'snapshot-secret-0001', allowedSubs: '00000000-0000-4000-8000-000000000001', label: 'Snapshot IdP', autoRedirect: false, passwordLogin: true, redirectUrl: 'https://vpn2.example.invalid/auth/authentik' }
  await adminCall('admin/sso.put', 'PUT', '/api/admin/sso', sso)
  await adminCall('admin/sso.put', 'PUT', '/api/admin/sso', { ...sso, enabled: true, clientId: '' })
  await adminCall('admin/sso.get', 'GET', '/api/admin/sso')
  await adminCall('admin/sso/check.post', 'POST', '/api/admin/sso/check', { authentikUrl: 'ftp://auth.example.invalid' })

  // Клиенты панели.
  const main = await adminCall('clients/index.post', 'POST', '/api/clients', { name: `${CLIENT_PREFIX}main`, filterTraffic: false, expiresAt: '2099-12-31T20:59:59.000Z', deviceLimit: 5 })
  vars.main = main.json?.id ?? 0
  const second = await adminCall('clients/index.post', 'POST', '/api/clients', { name: `${CLIENT_PREFIX}second` })
  vars.second = second.json?.id ?? 0
  await adminCall('clients/index.post', 'POST', '/api/clients', { name: '' })
  await adminCall('clients/index.get', 'GET', '/api/clients')
  await adminCall('clients/[id].get', 'GET', '/api/clients/{second}')
  vars.missing = MISSING_ID
  await adminCall('clients/[id].get', 'GET', '/api/clients/{missing}')

  const phone = await adminCall('clients/[id]/devices/index.post', 'POST', '/api/clients/{main}/devices', { name: 'phone' })
  vars.phone = phone.json?.id ?? phone.json?.device?.id ?? 0
  const laptop = await adminCall('clients/[id]/devices/index.post', 'POST', '/api/clients/{main}/devices', { name: 'laptop' })
  vars.laptop = laptop.json?.id ?? laptop.json?.device?.id ?? 0
  await adminCall('clients/[id]/devices/index.post', 'POST', '/api/clients/{main}/devices', { name: 'phone' })
  await adminCall('clients/[id].get', 'GET', '/api/clients/{main}')

  await adminCall('clients/[id]/devices/[deviceId]/wg-config.get', 'GET', '/api/clients/{main}/devices/{phone}/wg-config')
  await adminCall('clients/[id]/devices/[deviceId]/ovpn-config.get', 'GET', '/api/clients/{main}/devices/{phone}/ovpn-config')
  await adminCall('clients/[id]/devices/[deviceId]/ikev2-credentials.get', 'GET', '/api/clients/{main}/devices/{phone}/ikev2-credentials')
  await adminCall('clients/[id]/devices/[deviceId]/wg-qrcode.svg.get', 'GET', '/api/clients/{main}/devices/{phone}/wg-qrcode.svg')
  await adminCall('clients/[id]/devices/[deviceId]/send-wg-to-tg.post', 'POST', '/api/clients/{main}/devices/{phone}/send-wg-to-tg')
  await adminCall('clients/[id]/devices/[deviceId]/send-ovpn-to-tg.post', 'POST', '/api/clients/{main}/devices/{phone}/send-ovpn-to-tg')
  await adminCall('clients/[id]/devices/[deviceId]/send-ikev2-to-tg.post', 'POST', '/api/clients/{main}/devices/{phone}/send-ikev2-to-tg')
  await adminCall('clients/[id]/send-password-to-tg.post', 'POST', '/api/clients/{main}/send-password-to-tg')

  await adminCall('clients/[id].patch', 'PATCH', '/api/clients/{main}', { deviceLimit: 6, expiresAt: '2099-12-30T20:59:59.000Z' })
  await settle((await adminCall('clients/[id].patch', 'PATCH', '/api/clients/{main}', { frozenManual: true })).json)
  await settle((await adminCall('clients/[id].patch', 'PATCH', '/api/clients/{main}', { frozenManual: false })).json)
  await adminCall('clients/[id].patch', 'PATCH', '/api/clients/{main}', { deviceLimit: 0 })
  await settle((await adminCall('clients/[id]/devices/[deviceId]/reissue.post', 'POST', '/api/clients/{main}/devices/{phone}/reissue')).json)
  await settle((await adminCall('clients/[id]/reissue.post', 'POST', '/api/clients/{main}/reissue')).json)
  await adminCall('clients/top.get', 'GET', '/api/clients/top?period=day')
  await adminCall('clients/top.get', 'GET', '/api/clients/top?period=week')
  await adminCall('clients/top.get', 'GET', '/api/clients/top?period=year')
  await adminCall('clients/traffic.get', 'GET', '/api/clients/traffic')

  // Бот: клиент, созданный из админского чата.
  const bot = await botCall('bot/admin/clients/index.post', 'POST', '/api/bot/admin/clients', { name: `${CLIENT_PREFIX}bot`, filterTraffic: true })
  vars.bot = bot.json?.id ?? 0
  await botCall('bot/admin/clients/index.post', 'POST', '/api/bot/admin/clients', { name: '' })
  await botCall('bot/admin/clients/index.get', 'GET', '/api/bot/admin/clients')
  const card = await botCall('bot/admin/clients/[id].get', 'GET', '/api/bot/admin/clients/{bot}')
  await botCall('bot/admin/clients/[id].get', 'GET', '/api/bot/admin/clients/{missing}')
  await botCall('bot/admin/clients/[id].patch', 'PATCH', '/api/bot/admin/clients/{bot}', { deviceLimit: 3 })
  await settle((await botCall('bot/admin/clients/[id].patch', 'PATCH', '/api/bot/admin/clients/{bot}', { frozenManual: true })).json)
  await settle((await botCall('bot/admin/clients/[id].patch', 'PATCH', '/api/bot/admin/clients/{bot}', { frozenManual: false })).json)
  await botCall('bot/link.post', 'POST', '/api/bot/link', { password: 'wrong-password-snapshot', chatId: CHAT_ID })
  await botCall('bot/link.post', 'POST', '/api/bot/link', { password: card.json?.password ?? '', chatId: CHAT_ID, username: 'snapshot_user' })
  vars.chat = CHAT_ID
  await botCall('bot/client.get', 'GET', '/api/bot/client?chatId={chat}')
  await botCall('bot/client.get', 'GET', '/api/bot/client?chatId=1')
  const bphone = await botCall('bot/devices/index.post', 'POST', '/api/bot/devices', { chatId: CHAT_ID, name: 'phone' })
  const bdevice = (/** @type {any} */ json, /** @type {string} */ name) => json?.devices?.find((/** @type {any} */ d) => d.name === name)?.id ?? 0
  vars.bphone = bdevice(bphone.json, 'phone')
  const blaptop = await botCall('bot/devices/index.post', 'POST', '/api/bot/devices', { chatId: CHAT_ID, name: 'laptop' })
  vars.blaptop = bdevice(blaptop.json, 'laptop')
  await botCall('bot/devices/index.post', 'POST', '/api/bot/devices', { chatId: CHAT_ID, name: 'phone' })
  await botCall('bot/devices/[deviceId]/wg-config.get', 'GET', '/api/bot/devices/{bphone}/wg-config?chatId={chat}')
  await botCall('bot/devices/[deviceId]/ovpn-config.get', 'GET', '/api/bot/devices/{bphone}/ovpn-config?chatId={chat}')
  await botCall('bot/devices/[deviceId]/wg-config.get', 'GET', '/api/bot/devices/{missing}/wg-config?chatId={chat}')
  const reissue = await botCall('bot/devices/[deviceId]/reissue.post', 'POST', '/api/bot/devices/{bphone}/reissue', { chatId: CHAT_ID })
  await settle(reissue.json)
  await botCall('bot/admin/access-operations/[id]/retry.post', 'POST', `/api/bot/admin/access-operations/${reissue.json?.operation?.id ?? MISSING_OPERATION}/retry`)
  await botCall('bot/admin/access-operations/[id]/retry.post', 'POST', `/api/bot/admin/access-operations/${MISSING_OPERATION}/retry`)
  await settle((await botCall('bot/devices/[deviceId].delete', 'DELETE', '/api/bot/devices/{blaptop}?chatId={chat}')).json)
  await botCall('bot/events.post', 'POST', '/api/bot/events', { type: 'telegram_rejected', status: 403, recipient: 'client' })
  await botCall('bot/events.post', 'POST', '/api/bot/events', { type: 'unknown' })
  await botCall('bot/events.post', 'POST', '/api/bot/events', { type: 'backup_failed', detail: 'snapshot' }, false)
  await botCall('ops/bot-snapshot.get', 'GET', '/api/ops/bot-snapshot')

  // Рассылка и очередь бота, отвязка чата.
  await adminCall('admin/broadcast.post', 'POST', '/api/admin/broadcast', { text: 'api-snapshot' })
  await adminCall('admin/bot-deliveries.get', 'GET', '/api/admin/bot-deliveries')
  await adminCall('clients/[id]/unlink.post', 'POST', '/api/clients/{bot}/unlink')

  // Удаления: устройство и клиенты.
  const del = await adminCall('clients/[id]/devices/[deviceId].delete', 'DELETE', '/api/clients/{main}/devices/{laptop}')
  await settle(del.json)
  const delMain = await adminCall('clients/[id].delete', 'DELETE', '/api/clients/{main}')
  await settle(delMain.json)
  await io.gone('/api/clients/' + vars.main)
  const delSecond = await botCall('bot/admin/clients/[id].delete', 'DELETE', '/api/bot/admin/clients/{second}')
  await settle(delSecond.json)
  await io.gone('/api/clients/' + vars.second)
  await settle((await botCall('bot/admin/clients/[id].delete', 'DELETE', '/api/bot/admin/clients/{bot}')).json)
  await io.gone('/api/clients/' + vars.bot)

  void raw
  return calls
}

/** @param {unknown} node @param {string[]} path @param {(value: unknown) => unknown} fn @returns {number} */
function mapPath(node, path, fn) {
  if (node === null || typeof node !== 'object') return 0
  const [head, ...rest] = path
  const holder = /** @type {Record<string, unknown>} */ (node)
  if (head === '*') {
    return Object.keys(holder).reduce((n, k) => n + (rest.length ? mapPath(holder[k], rest, fn) : (holder[k] = fn(holder[k]), 1)), 0)
  }
  if (!(head in holder)) return 0
  if (rest.length) return mapPath(holder[head], rest, fn)
  holder[head] = fn(holder[head])
  return 1
}

/**
 * Применяет таблицу к вызовам маршрута. Значение, не подошедшее под check,
 * и применимая запись, которая ничего не нашла, валят прогон.
 * @param {string} route
 * @param {Call[]} list
 * @returns {Call[]}
 */
export function normalize(route, list) {
  const rules = NORMALIZATION.filter(r => r.route === route)
  const out = structuredClone(list)
  for (const call of out) {
    for (const rule of rules.filter(r => r.status === undefined || r.status === call.status)) {
      /** @type {(v: unknown) => unknown} */
      const swap = (v) => {
        if (!rule.check.test(String(v))) throw new Error(`${route} ${rule.key}: «${String(v).slice(0, 40)}» не подходит под ${rule.check}`)
        return rule.label
      }
      if (rule.kind === 'field') mapPath(call.body, rule.key.split('.'), swap)
      else if (rule.kind === 'body') call.body = swap(call.body)
      else if (typeof call.body === 'string') {
        const text = call.body
        if (rule.kind === 'line') {
          call.body = text.replace(new RegExp(`^(${rule.key}\\s*=\\s*)(.*)$`, 'gm'), (_, head, value) => head + swap(value))
        } else {
          call.body = text.replace(new RegExp(`(<${rule.key}>\\n)([\\s\\S]*?)(\\n</${rule.key}>)`, 'g'), (_, a, value, c) => a + swap(value) + c)
        }
      }
    }
  }
  return out
}

/**
 * Эталонные строки файлов по маршрутам: JSON с отступом и переводом строки в конце.
 * @param {Map<string, Call[]>} calls
 * @returns {Map<string, string>}
 */
export function snapshotFiles(calls) {
  return new Map([...calls].map(([route, list]) => [route, `${JSON.stringify(normalize(route, list), null, 2)}\n`]))
}
