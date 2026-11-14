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
 *   initialized: (clientIds: (string | number)[]) => Promise<void>,
 *   gone: (path: string) => Promise<void>,
 *   delivered: (deliveryIds: string[]) => Promise<void>,
 * }} Io
 * @typedef {{ method: string, path: string, status: number, contentType: string, body: unknown }} Call
 */

/**
 * Таблица нормализации: маршрут (* - любой), вид, поле, метка, формат исходного
 * значения, причина. Вид: field - поле JSON (путь через точку, * - любой ключ
 * или элемент), line - значение строки `Ключ = значение`, block - тело блока
 * <тег>…</тег>, body - всё тело, scope - оставить в массиве (key - путь, пусто
 * для корня) только элементы, у которых поле field (первое слово check, до
 * «:») подходит под регулярное выражение, idkeys - оставить в объекте только
 * ключи-идентификаторы клиентов сценария, delta - в первом вызове маршрута
 * (база до действий сценария) поле заменяется меткой, в остальных - разницей
 * с базой вида +N. Значение null остаётся null. Формат
 * (check) проверяется на каждом значении: не подошёл - прогон падает, так
 * длина и вид случайных значений проверены отдельно от эталона. Поля вне
 * таблицы сравниваются как есть.
 * @typedef {{ route: string, kind: 'field' | 'line' | 'block' | 'body' | 'scope' | 'idkeys' | 'delta', key: string, label: string, check: RegExp, reason: string, field?: string }} Rule
 */

const ID = /^\d+$/
const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$/
const MS = /^\d{13}$/
const SEC = /^\d{10}$/
const PERCENT = /^(100|\d{1,2}(\.\d+)?)$/
const RATE = /^\d+(\.\d+)?$/
const UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/
const PASSWORD = /^[A-Za-z0-9]{20}$/
const KEY44 = /^[A-Za-z0-9+/]{43}=$/
const PEM = /^(-----BEGIN [A-Z0-9 ]+-----)\n[\s\S]+\n-----END [A-Z0-9 ]+-----\n?$/
const OVPN_KEY = /^-----BEGIN [A-Za-z0-9 ]+-----\n[\s\S]+\n-----END [A-Za-z0-9 ]+-----$/

/**
 * @param {string[]} routes
 * @param {Rule['kind']} kind
 * @param {string[]} keys
 * @param {string} label
 * @param {RegExp} check
 * @param {string} reason
 * @returns {Rule[]}
 */
const rule = (routes, kind, keys, label, check, reason) =>
  routes.flatMap(route => keys.map(key => ({ route, kind, key, label, check, reason })))

const R_ID = 'идентификатор из автоинкремента: зависит от клиентов других спеков в шарде'
const R_TIME = 'время создания: момент прогона'
const R_PASSWORD = 'пароль клиента генерируется случайно'
const R_OP = 'операция доступа: случайный uuid и моменты прогона'
const BOT_CLIENT = ['bot/link.post', 'bot/client.get', 'bot/devices/index.post', 'bot/devices/[deviceId]/reissue.post', 'bot/devices/[deviceId].delete']
const WG_TEXT = ['clients/[id]/devices/[deviceId]/wg-config.get', 'bot/devices/[deviceId]/wg-config.get']
const OVPN_TEXT = ['clients/[id]/devices/[deviceId]/ovpn-config.get', 'bot/devices/[deviceId]/ovpn-config.get']
const SNAPSHOT = 'ops/bot-snapshot.get'
const R_FRESH = 'подтверждение старше 120 с - «не подтверждено» (shared/access-operation.ts:37): списки читаются в конце обхода, его длительность разная'
const ACCESS_STATUS = /^(Доступ включён|Доступ отключён|Ключи перевыпущены|Состояние не подтверждено)$/

/** @type {Rule[]} */
const NORMALIZATION = [
  ...rule(['*'], 'field', ['url'], '<url>', /^http:\/\/127\.0\.0\.1:\d+\/api\//, 'адрес в теле ошибки: порт стенда и идентификаторы'),
  ...rule(['*'], 'field', ['operation.id'], '<uuid>', UUID, R_OP),
  ...rule(['*'], 'field', ['operation.createdAt', 'operation.nextRetry', 'operation.protocols.*.nextRetry', 'operation.protocols.*.checkedAt', 'operation.protocols.*.lastAttempt'], '<ms>', MS, R_OP),
  ...rule(['*'], 'field', ['operation.clientId', 'operation.deviceId'], '<id>', ID, R_ID),

  ...rule(['admin/telegram.get'], 'delta', ['clients_total', 'clients_subscribed'], '<count>', ID, 'первый вызов - число клиентов шарда до сценария, дальше разница со сценарием: спеки панели идут по одному'),
  ...rule(['admin/telegram.get'], 'field', ['queued', 'delivered_day'], '<count>', ID, 'очередь шарда: доставки соседних спеков дозревают повтором через 30 с (server/utils/bot-events.ts:10,91)'),

  ...rule(['clients/index.post', 'clients/[id].patch', 'bot/admin/clients/index.post', 'bot/admin/clients/[id].patch'], 'field', ['id'], '<id>', ID, R_ID),
  ...rule(['clients/index.post', 'clients/[id].patch', 'bot/admin/clients/index.post', 'bot/admin/clients/[id].patch'], 'field', ['createdAt', 'updatedAt'], '<time>', ISO, R_TIME),
  ...rule(['clients/index.post', 'clients/[id].patch', 'bot/admin/clients/index.post', 'bot/admin/clients/[id].patch', 'bot/admin/clients/[id].get'], 'field', ['password'], '<password>', PASSWORD, R_PASSWORD),
  ...rule(['bot/admin/clients/[id].get'], 'field', ['id'], '<id>', ID, R_ID),
  ...rule(['bot/admin/clients/[id].get', ...BOT_CLIENT], 'field', ['createdAt'], '<time>', ISO, R_TIME),

  { route: 'clients/index.get', kind: 'scope', key: '', field: 'name', label: '', check: /^e2e-api-snapshot-/, reason: 'список общий на панель шарда: строки других спеков из эталона исключены' },
  ...rule(['clients/index.get'], 'field', ['*.id'], '<id>', ID, R_ID),
  ...rule(['clients/index.get'], 'field', ['*.createdAt'], '<time>', ISO, R_TIME),
  ...rule(['clients/index.get'], 'field', ['*.accessStatus'], '<access-status>', ACCESS_STATUS, R_FRESH),
  ...rule(['clients/[id].get'], 'field', ['id', 'devices.*.id'], '<id>', ID, R_ID),
  ...rule(['clients/[id].get'], 'field', ['createdAt', 'devices.*.createdAt'], '<time>', ISO, R_TIME),
  ...rule(['clients/[id].get', ...BOT_CLIENT], 'field', ['devices.*.configName'], '<config-name>', /^e2e-api-[a-z-]*-[0-9a-z]+$/, 'имя конфига содержит идентификатор устройства в base36'),
  ...rule(['clients/[id]/devices/index.post'], 'field', ['id', 'clientId'], '<id>', ID, R_ID),
  ...rule(['clients/[id]/devices/index.post'], 'field', ['createdAt'], '<time>', ISO, R_TIME),

  ...rule(WG_TEXT, 'line', ['PrivateKey', 'PublicKey', 'PresharedKey'], '<wg-key>', KEY44, 'ключ WireGuard: случайный (серверный - свой на каждый стенд)'),
  ...rule(WG_TEXT, 'line', ['Address'], '<address>', /^\d+\.\d+\.\d+\.\d+\/\d+, [0-9a-f:]+\/\d+$/, 'адрес из пула: зависит от устройств других спеков'),
  ...rule(OVPN_TEXT, 'block', ['ca', 'cert'], '<pem>', PEM, 'сертификат OpenVPN: случайный, срок от момента прогона'),
  ...rule(OVPN_TEXT, 'block', ['key', 'tls-crypt'], '<secret>', OVPN_KEY, 'закрытый ключ и tls-crypt: случайные'),
  ...rule(['clients/[id]/devices/[deviceId]/ikev2-credentials.get'], 'field', ['password'], '<password>', PASSWORD, R_PASSWORD),
  ...rule(['clients/[id]/devices/[deviceId]/ikev2-credentials.get'], 'field', ['caCertPem'], '<pem>', PEM, 'сертификат IKEv2 CA: случайный, срок от момента прогона'),
  { route: 'clients/[id]/devices/[deviceId]/wg-qrcode.svg.get', kind: 'body', key: '', label: '<svg-qrcode>', check: /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" viewBox="0 0 \d+ \d+"[\s\S]+<\/svg>$/, reason: 'картинка кодирует случайные ключи WireGuard' },
  // Тело PNG читается как текст UTF-8: байт 0x89 сигнатуры становится U+FFFD, остальное цело; управляющие байты после сигнатуры заменены в check любым символом (no-control-regex).
  { route: 'bot/devices/[deviceId]/wg-qrcode.png.get', kind: 'body', key: '', label: '<png-qrcode>', check: /^\uFFFDPNG\r\n[\s\S]\n[\s\S]{3}\rIHDR[\s\S]{60,}$/, reason: 'картинка кодирует случайные ключи WireGuard: проверены сигнатура PNG, заголовок IHDR и длина' },

  ...rule(['clients/top.get'], 'field', ['since'], '<sec>', SEC, 'начало суток: от даты прогона'),
  ...rule(['clients/top.get'], 'field', ['historyStart'], '<sec>', SEC, 'начало истории: от момента прогона'),
  ...rule(['clients/top.get'], 'delta', ['counts.total', 'counts.onlineNow', 'counts.activeDay', 'counts.activeWeek'], '<count>', ID, 'первый вызов - счётчики шарда до сценария, дальше разница со сценарием: спеки панели идут по одному'),
  { route: 'clients/traffic.get', kind: 'idkeys', key: '', label: '', check: ID, reason: 'карта трафика общая на панель: оставлены клиенты сценария, ключ-идентификатор заменён именем' },

  { route: 'bot/admin/clients/index.get', kind: 'scope', key: '', field: 'name', label: '', check: /^e2e-api-snapshot-/, reason: 'список общий на панель шарда: строки других спеков из эталона исключены' },
  ...rule(['bot/admin/clients/index.get'], 'field', ['*.id'], '<id>', ID, R_ID),
  ...rule(['bot/admin/clients/index.get'], 'field', ['*.createdAt'], '<time>', ISO, R_TIME),
  ...rule(['bot/admin/clients/index.get'], 'field', ['*.accessStatus'], '<access-status>', ACCESS_STATUS, R_FRESH),
  ...rule(BOT_CLIENT, 'field', ['id', 'devices.*.id'], '<id>', ID, R_ID),

  { route: SNAPSHOT, kind: 'scope', key: 'clients', field: 'name', label: '', check: /^e2e-api-snapshot-/, reason: 'список клиентов общий на панель шарда: строки других спеков из эталона исключены' },
  ...rule([SNAPSHOT], 'field', ['timestamp'], '<time>', ISO, R_TIME),
  ...rule([SNAPSHOT], 'field', ['clients.*.id'], '<id>', ID, R_ID),
  ...rule([SNAPSHOT], 'field', ['clients.*.createdAt'], '<time>', ISO, R_TIME),
  ...rule([SNAPSHOT], 'field', ['nodes.*.cpu', 'nodes.*.ram'], '<percent>', PERCENT, 'загрузка подставной VM: другие спеки меняют её в шарде, проверен диапазон 0..100'),
  ...rule([SNAPSHOT], 'field', ['nodes.*.rxMbps', 'nodes.*.txMbps'], '<mbps>', RATE, 'скорость подставной VM: другие спеки меняют её в шарде, проверено неотрицательное число'),
  ...rule([SNAPSHOT], 'field', ['nodes.*.uptimeSec', 'nodes.*.staleSec', 'outbounds.*.rttMs', 'connections'], '<count>', ID, 'время работы, давность, задержка и соединения подставных VM и clash: от момента опроса и других спеков, проверено целое неотрицательное'),

  ...rule(['admin/broadcast.post'], 'field', ['recipients'], '<count>', ID, 'число привязанных чатов шарда: зависит от других спеков'),
  ...rule(['admin/broadcast.post'], 'field', ['delivery.id'], '<uuid>', UUID, 'идентификатор доставки: случайный'),
  ...rule(['admin/broadcast.post'], 'field', ['delivery.created'], '<ms>', MS, R_TIME),
  ...rule(['admin/broadcast.post'], 'field', ['delivery.status'], '<first-attempt>', /^(accepted|queued)$/, 'итог первой попытки: бот за 1,5 с под сменой сети CI может не ответить (server/utils/bot-events.ts:76,91); конечное accepted сценарий дожидается до admin/bot-deliveries.get'),
  ...rule(['admin/broadcast.post'], 'field', ['delivery.remaining'], '<first-attempt>', /^(0|5)$/, 'остаток попыток после первой: 0 при accepted, 5 при queued (server/utils/bot-events.ts:18)'),
  { route: 'admin/bot-deliveries.get', kind: 'scope', key: '', field: 'label', label: '', check: /e2e-api-snapshot-/, reason: 'очередь общая на панель шарда: доставки других спеков из эталона исключены' },
  ...rule(['admin/bot-deliveries.get'], 'field', ['*.id'], '<uuid>', UUID, 'идентификатор доставки: случайный'),
  ...rule(['admin/bot-deliveries.get'], 'field', ['*.created'], '<ms>', MS, R_TIME),
]

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
 * @returns {Promise<{ calls: Map<string, Call[]>, ids: Record<string, string | number> }>}
 */
export async function walk(io) {
  /** @type {Map<string, Call[]>} */
  const calls = new Map()
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
  const sso = { enabled: false, authentikUrl: 'https://auth.example.invalid', appSlug: 'vpn2-snapshot', clientId: 'vpn2-snapshot', clientSecret: 'snap-secret01', allowedSubs: '00000000-0000-4000-8000-000000000001', label: 'Snapshot IdP', autoRedirect: false, passwordLogin: true, redirectUrl: 'https://vpn2.example.invalid/auth/authentik' }
  await adminCall('admin/sso.put', 'PUT', '/api/admin/sso', sso)
  await adminCall('admin/sso.put', 'PUT', '/api/admin/sso', { ...sso, enabled: true, clientId: '' })
  await adminCall('admin/sso.get', 'GET', '/api/admin/sso')
  await adminCall('admin/sso/check.post', 'POST', '/api/admin/sso/check', { authentikUrl: 'ftp://auth.example.invalid' })

  // Клиенты панели. Первый top.get - база счётчиков до клиентов сценария.
  await adminCall('clients/top.get', 'GET', '/api/clients/top?period=day')
  const main = await adminCall('clients/index.post', 'POST', '/api/clients', { name: `${CLIENT_PREFIX}main`, filterTraffic: false, expiresAt: '2099-12-31T20:59:59.000Z', deviceLimit: 5 })
  vars.main = main.json?.id ?? 0
  const second = await adminCall('clients/index.post', 'POST', '/api/clients', { name: `${CLIENT_PREFIX}second` })
  vars.second = second.json?.id ?? 0
  await adminCall('clients/index.post', 'POST', '/api/clients', { name: '' })
  // Клиент из админского чата бота.
  const bot = await botCall('bot/admin/clients/index.post', 'POST', '/api/bot/admin/clients', { name: `${CLIENT_PREFIX}bot`, filterTraffic: true })
  vars.bot = bot.json?.id ?? 0
  // Первичную сверку нового клиента заводит cron панели раз в 60 с (server/plugins/cron.ts):
  // чтения - после неё, иначе ответ зависит от того, успел ли тик.
  await io.initialized([vars.main, vars.second, vars.bot])
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

  // Бот: клиенты из админского чата.
  await botCall('bot/admin/clients/index.post', 'POST', '/api/bot/admin/clients', { name: '' })
  await botCall('bot/admin/clients/index.get', 'GET', '/api/bot/admin/clients')
  const card = await botCall('bot/admin/clients/[id].get', 'GET', '/api/bot/admin/clients/{bot}')
  await botCall('bot/admin/clients/[id].get', 'GET', '/api/bot/admin/clients/{missing}')
  await botCall('bot/admin/clients/[id].patch', 'PATCH', '/api/bot/admin/clients/{bot}', { deviceLimit: 3 })
  await settle((await botCall('bot/admin/clients/[id].patch', 'PATCH', '/api/bot/admin/clients/{bot}', { frozenManual: true })).json)
  await settle((await botCall('bot/admin/clients/[id].patch', 'PATCH', '/api/bot/admin/clients/{bot}', { frozenManual: false })).json)
  await botCall('bot/link.post', 'POST', '/api/bot/link', { password: 'wrong-pass', chatId: CHAT_ID })
  await botCall('bot/link.post', 'POST', '/api/bot/link', { password: card.json?.password ?? '', chatId: CHAT_ID, username: 'snapshot_user' })
  vars.chat = CHAT_ID
  await botCall('bot/client.get', 'GET', '/api/bot/client?chatId={chat}')
  await botCall('bot/client.get', 'GET', '/api/bot/client?chatId=1')
  // Три клиента сценария, один с привязанным чатом.
  await adminCall('admin/telegram.get', 'GET', '/api/admin/telegram')
  const bphone = await botCall('bot/devices/index.post', 'POST', '/api/bot/devices', { chatId: CHAT_ID, name: 'phone' })
  const bdevice = (/** @type {any} */ json, /** @type {string} */ name) => json?.devices?.find((/** @type {any} */ d) => d.name === name)?.id ?? 0
  vars.bphone = bdevice(bphone.json, 'phone')
  const blaptop = await botCall('bot/devices/index.post', 'POST', '/api/bot/devices', { chatId: CHAT_ID, name: 'laptop' })
  vars.blaptop = bdevice(blaptop.json, 'laptop')
  await botCall('bot/devices/index.post', 'POST', '/api/bot/devices', { chatId: CHAT_ID, name: 'phone' })
  await botCall('bot/devices/[deviceId]/wg-config.get', 'GET', '/api/bot/devices/{bphone}/wg-config?chatId={chat}')
  await botCall('bot/devices/[deviceId]/wg-qrcode.png.get', 'GET', '/api/bot/devices/{bphone}/wg-qrcode.png?chatId={chat}')
  await botCall('bot/devices/[deviceId]/ovpn-config.get', 'GET', '/api/bot/devices/{bphone}/ovpn-config?chatId={chat}')
  await botCall('bot/devices/[deviceId]/wg-config.get', 'GET', '/api/bot/devices/{missing}/wg-config?chatId={chat}')
  const reissue = await botCall('bot/devices/[deviceId]/reissue.post', 'POST', '/api/bot/devices/{bphone}/reissue', { chatId: CHAT_ID })
  await settle(reissue.json)
  vars.operation = reissue.json?.operation?.id ?? MISSING_OPERATION
  vars.nooperation = MISSING_OPERATION
  await botCall('bot/admin/access-operations/[id]/retry.post', 'POST', '/api/bot/admin/access-operations/{operation}/retry')
  await botCall('bot/admin/access-operations/[id]/retry.post', 'POST', '/api/bot/admin/access-operations/{nooperation}/retry')
  const dropLaptop = await botCall('bot/devices/[deviceId].delete', 'DELETE', '/api/bot/devices/{blaptop}?chatId={chat}')
  await settle(dropLaptop.json)
  await botCall('bot/events.post', 'POST', '/api/bot/events', { type: 'telegram_rejected', status: 403, recipient: 'client' })
  await botCall('bot/events.post', 'POST', '/api/bot/events', { type: 'unknown' })
  await botCall('bot/events.post', 'POST', '/api/bot/events', { type: 'backup_failed', detail: 'snapshot' }, false)
  await botCall('ops/bot-snapshot.get', 'GET', '/api/ops/bot-snapshot')

  // Рассылка и очередь бота, отвязка чата.
  const broadcast = await adminCall('admin/broadcast.post', 'POST', '/api/admin/broadcast', { text: 'api-snapshot' })
  // Уведомление клиенту о подтверждённой операции ставится в очередь под номером
  // операции и видно в списке только после первой попытки таймера: запись очереди
  // ждёт и рассылку, и оба уведомления привязанного чата.
  await io.delivered([broadcast, reissue, dropLaptop].map(r => r.json?.delivery?.id ?? r.json?.operation?.id ?? MISSING_OPERATION))
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
  // Клиенты сценария удалены: счётчики вернулись к базе.
  await adminCall('admin/telegram.get', 'GET', '/api/admin/telegram')

  return { calls, ids: { main: vars.main, second: vars.second, bot: vars.bot } }
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
 * валит прогон. Правило, которое ничего не нашло, допустимо: одно правило
 * покрывает несколько вызовов и состояний.
 * @param {string} route
 * @param {Call[]} list
 * @param {Record<string, string | number>} ids идентификаторы клиентов сценария
 * @returns {Call[]}
 */
function normalize(route, list, ids) {
  const rules = NORMALIZATION.filter(r => r.route === route || r.route === '*')
  const out = structuredClone(list)
  const order = [...rules.filter(r => r.kind === 'scope' || r.kind === 'idkeys'), ...rules.filter(r => r.kind !== 'scope' && r.kind !== 'idkeys')]
  for (const call of out) {
    for (const rule of order) {
      /** @type {(v: unknown) => unknown} */
      const swap = (v) => {
        if (v === null) return null
        if (!rule.check.test(String(v))) throw new Error(`${route} ${rule.key}: «${String(v).slice(0, 40)}» не подходит под ${rule.check}`)
        return rule.label
      }
      if (rule.kind === 'scope') {
        const holder = /** @type {any} */ (rule.key ? rule.key.split('.').reduce((n, k) => n?.[k], /** @type {any} */ (call.body)) : call.body)
        if (!Array.isArray(holder) || call.status !== 200) continue
        const kept = holder.filter(item => rule.check.test(String(item?.[rule.field ?? ''] ?? '')))
        holder.splice(0, holder.length, ...kept)
      }
      else if (rule.kind === 'idkeys') {
        if (call.status !== 200 || call.body === null || typeof call.body !== 'object') continue
        const body = /** @type {Record<string, unknown>} */ (call.body)
        const named = Object.entries(ids).map(([name, id]) => /** @type {[string, string]} */ ([`{${name}}`, String(id)]))
        call.body = Object.fromEntries(named.filter(([, id]) => id in body).map(([name, id]) => [name, body[id]]))
      }
      else if (rule.kind === 'delta') {
        if (call.status !== 200) continue
        const base = /** @type {any} */ (rule.key.split('.').reduce((n, k) => n?.[k], /** @type {any} */ (list[0]?.body)))
        const first = call === out[0]
        mapPath(call.body, rule.key.split('.'), (v) => {
          if (!rule.check.test(String(v)) || !rule.check.test(String(base))) throw new Error(`${route} ${rule.key}: «${String(v)}» или база «${String(base)}» не подходит под ${rule.check}`)
          const d = Number(v) - Number(base)
          return first ? rule.label : `${d < 0 ? '' : '+'}${d}`
        })
      }
      else if (rule.kind === 'field') mapPath(call.body, rule.key.split('.'), swap)
      else if (rule.kind === 'body') call.body = swap(call.body)
      else if (typeof call.body === 'string') {
        const text = call.body
        if (rule.kind === 'line') {
          call.body = text.replace(new RegExp(`^(${rule.key}\\s*=\\s*)(.*)$`, 'gm'), (_, head, value) => head + swap(value))
        }
        else {
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
 * @param {Record<string, string | number>} ids
 * @returns {Map<string, string>}
 */
export function snapshotFiles(calls, ids) {
  return new Map([...calls].map(([route, list]) => [route, `${JSON.stringify(normalize(route, list, ids), null, 2)}\n`]))
}
