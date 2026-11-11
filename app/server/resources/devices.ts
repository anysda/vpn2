import { and, eq } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { z } from 'zod'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { accessRequestKey, enqueueAccess } from '../utils/access-operations'
import { accepted, admin, bot, idParam, notFound, recordIds, validId } from '../utils/api'
import { botClientView, clientByChat, requireChatLinked } from '../utils/bot-api'
import { botSecretOrThrow, notifyClient, notifyWarning, sendBotEvent } from '../utils/bot-events'
import { clientDns } from '../utils/client-dns'
import { syncClientPrefsFile } from '../utils/client-prefs'
import { createDeviceWithAddresses, deviceRequestKey, provisionDeviceKeys, rejectDeletedDeviceRepeat } from '../utils/create-device'
import { actorOf } from '../utils/events'
import { buildIkev2ClientInfo, entryHost, ikev2CaReady, ikev2Mode, ikev2ServerReady, readIkev2CaPem } from '../utils/ikev2'
import { deviceConfigName } from '../utils/naming'
import { buildOvpnConfig, caReady, ensureDeviceOvpn } from '../utils/openvpn'
import { qrPng, qrSvg, wgBotEvent } from '../utils/qr'
import { readBodyAs } from '../utils/validate'
import { buildWgClientConfig, ensureDeviceWg, loadServerKeys, syncWireguardConfig } from '../utils/wireguard'

type Client = typeof clients.$inferSelect
type Device = typeof devices.$inferSelect
/** Проверки между разбором адреса и поиском в базе (протокол, секрет бота); результат отдаётся как `ready`. */
type Preflight<T> = () => T | Promise<T>

const Name = z.string().trim().min(1).max(64)
const ChatId = z.number().int()

async function deviceOf(clientId: number, deviceId: number): Promise<Device> {
  const [device] = await useDb().select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw notFound()
  return device
}

/** Девайс клиента для маршрутов панели `clients/:id/devices/:deviceId/*`. */
async function panelDevice<T = undefined>(event: H3Event, preflight?: Preflight<T>) {
  const { id, deviceId } = recordIds(event, 'id', 'deviceId')
  const ready = await preflight?.() as T
  const [client] = await useDb().select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw notFound()
  return { client, device: await deviceOf(id, deviceId), ready }
}

/**
 * Девайс клиента, привязанного к чату, для маршрутов бота `bot/devices/:deviceId/*`.
 * Ответ с секретом перепроверяет привязку сам (`botConfig`).
 */
async function botDevice<T = undefined>(event: H3Event, preflight?: Preflight<T>) {
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!validId(deviceId) || !Number.isSafeInteger(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }
  const ready = await preflight?.() as T
  const client = await clientByChat(chatId)
  return { client, device: await deviceOf(client.id, deviceId), ready, chatId }
}

/** Проверки до поиска в базе: WireGuard включён и публичный адрес задан. */
export function requireWgEndpoint(): string {
  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) throw createError({ statusCode: 503, statusMessage: 'wg_disabled', message: 'WireGuard выключен' })
  const endpoint = String(cfg.wgPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'wg_public_host_not_configured' })
  return endpoint
}

/** Клиентский конфиг WireGuard девайса; ключи девайса выдаются при первом обращении. */
async function wgConfig(device: Device, client: Client, endpoint: string): Promise<string> {
  const server = await loadServerKeys().catch(() => null)
  if (!server) {
    throw createError({ statusCode: 503, statusMessage: 'wg_keys_not_ready', message: 'WireGuard server-ключи ещё не инициализированы (стадия 28-wireguard)' })
  }
  const dev = await ensureDeviceWg(device.id)
  await syncWireguardConfig().catch(() => {})
  const cfg = useRuntimeConfig()
  return buildWgClientConfig({
    clientPrivateKey: dev.wgPrivateKey!,
    clientPresharedKey: dev.wgPresharedKey!,
    clientIp: dev.wgIp!,
    serverPublicKey: server.publicKey,
    serverEndpoint: endpoint,
    serverPort: Number(cfg.wgListenPort),
    dns: clientDns(client.filterTraffic).join(', '),
    mtu: Number(cfg.wgMtu),
    splitLocal: cfg.wgSplitLocal !== false,
    tunnelPrefixes: [String(cfg.wgSubnetPrefix), String(cfg.mgmtMeshIpPrefix)],
  })
}

/**
 * Проверки до поиска в базе: OpenVPN включён, публичный адрес задан, CA готов.
 * `beforeCa` - проверка маршрута между адресом и готовностью CA (секрет бота у отправки в Telegram).
 */
async function requireOvpnEndpoint(beforeCa?: () => void): Promise<string> {
  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) throw createError({ statusCode: 503, statusMessage: 'ovpn_disabled', message: 'OpenVPN выключен' })
  const endpoint = String(cfg.ovpnPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'ovpn_public_host_not_configured' })
  beforeCa?.()
  if (!(await caReady())) {
    throw createError({ statusCode: 503, statusMessage: 'ovpn_ca_not_ready', message: 'OpenVPN CA ещё не инициализирован (стадия 29-openvpn)' })
  }
  return endpoint
}

/** Клиентский конфиг OpenVPN девайса; сертификат девайса выдаётся при первом обращении. */
async function ovpnConfig(device: Device, client: Client, endpoint: string): Promise<string> {
  const cfg = useRuntimeConfig()
  const dev = await ensureDeviceOvpn(device.id)
  return buildOvpnConfig({
    clientCert: dev.ovpnCert!,
    clientKey: dev.ovpnKey!,
    serverHost: endpoint,
    serverPort: Number(cfg.ovpnPort),
    proto: String(cfg.ovpnProto),
    dns: clientDns(client.filterTraffic),
    splitLocal: cfg.wgSplitLocal !== false,
    tunnelPrefixes: [String(cfg.mgmtMeshIpPrefix)],
  })
}

/**
 * Проверки IKEv2 до поиска в базе: сервер развёрнут, адрес entry задан (серверный
 * endpoint - IP entry, TZ §14.1). `beforeHost` - секрет бота у отправки в Telegram.
 */
async function requireIkev2(beforeHost?: () => void) {
  if (!(await ikev2ServerReady())) {
    throw createError({ statusCode: 503, statusMessage: 'ikev2_not_deployed', message: 'IKEv2-сервер ещё не развёрнут (стадия 27-ikev2)' })
  }
  beforeHost?.()
  if (!entryHost()) throw createError({ statusCode: 500, statusMessage: 'entry_host_not_configured' })
}

type Config = (device: Device, client: Client, endpoint: string) => Promise<string>

const panelConfig = async (event: H3Event, preflight: Preflight<string>, build: Config) => {
  const { client, device, ready } = await panelDevice(event, preflight)
  return { client, device, conf: await build(device, client, ready) }
}
const panelWg = async (event: H3Event) => (await panelConfig(event, requireWgEndpoint, wgConfig)).conf

/** Конфиг по запросу клиента из бота. Привязка чата - последний await перед ответом: отвязанный за время сборки чат конфиг не получает. */
async function botConfig(event: H3Event, preflight: Preflight<string>, build: Config) {
  const { client, device, chatId, ready } = await botDevice(event, preflight)
  const conf = await build(device, client, ready)
  await requireChatLinked(client.id, chatId)
  return conf
}

function typed<T>(event: H3Event, type: string, body: T): T {
  setHeader(event, 'content-type', type)
  return body
}
const text = (event: H3Event, body: string) => typed(event, 'text/plain; charset=utf-8', body)

/**
 * Создание девайса панелью и ботом: один путь от ключа идемпотентности до
 * синхронизации протоколов. Владельца ищет `owner`; `guard` перепроверяет его
 * строку, имя и лимит внутри транзакции вставки - прочитанное раньше могло
 * устареть, пока шли await. Ключи выдаются сразу: девайс готов к выдаче конфигов.
 */
async function addDevice(
  event: H3Event, name: string, request: Record<string, unknown>, owner: () => Promise<Client>,
  texts: { exists: string, limit: (limit: number) => string }, guard: (fresh: Client) => void,
) {
  const idempotency = deviceRequestKey(event, request)
  await rejectDeletedDeviceRepeat(idempotency)
  const client = await owner()
  const cfg = useRuntimeConfig()
  const ovpnReady = cfg.ovpnEnabled && await caReady()
  const created = await createDeviceWithAddresses(client.id, name, { wg: cfg.wgEnabled, ovpn: ovpnReady }, (names, fresh) => {
    guard(fresh)
    if (names.includes(name)) throw createError({ statusCode: 409, statusMessage: 'device_exists', message: texts.exists })
    if (fresh.deviceLimit != null && names.length >= fresh.deviceLimit) {
      throw createError({ statusCode: 409, statusMessage: 'device_limit', message: texts.limit(fresh.deviceLimit) })
    }
  }, idempotency)
  await provisionDeviceKeys(created.device.id, ovpnReady)
  await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))
  return { ...created, client }
}

/** Операция доступа над своим девайсом из бота: ответ - сводка клиента с операцией. */
async function botAccess(event: H3Event, kind: 'delete' | 'reissue', chat: () => number | Promise<number>) {
  const invalid = () => createError({ statusCode: 400, statusMessage: 'invalid_params' })
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!validId(deviceId)) throw invalid()
  const chatId = await chat()
  if (!Number.isSafeInteger(chatId)) throw invalid()
  const client = await clientByChat(chatId)
  const { operation } = await enqueueAccess({ clientId: client.id, chatId, deviceId, kind, actor: 'bot', key: accessRequestKey(event) })
  return accepted(event, { ...await botClientView(client.id), operation })
}

export default {
  /** Добавить девайс клиенту. */
  'POST /clients/:id/devices': admin(async (event) => {
    const clientId = idParam(event)
    const { name } = await readBodyAs(event, z.object({ name: Name }))
    // Адресат уведомления - из строки клиента в транзакции вставки: бота могли отвязать.
    let owner!: Client
    const { device, replay } = await addDevice(event, name, { source: 'panel', clientId, name }, async () => {
      const [client] = await useDb().select().from(clients).where(eq(clients.id, clientId)).limit(1)
      if (!client) throw notFound()
      return client
    }, { exists: `Девайс «${name}» у клиента уже есть`, limit: limit => `Достигнут лимит девайсов (${limit})` }, (fresh) => { owner = fresh })
    // Устройство уже создано: сбой очереди бота — предупреждение, не ошибка.
    // Повтор по ключу идемпотентности клиента уже уведомил.
    const warning = !replay && owner?.tgChatId
      ? await notifyWarning(notifyClient(
          owner.tgChatId,
          `➕ Администратор добавил вам устройство «${name}». `
          + 'Откройте «📱 Мои устройства», чтобы получить конфиг.',
          owner.name,
        ))
      : undefined
    return { id: device.id, clientId, name: device.name, createdAt: device.createdAt, ...(warning ? { warning } : {}) }
  }),
  'DELETE /clients/:id/devices/:deviceId': admin(async (event, user) => {
    const { id, deviceId } = recordIds(event, 'id', 'deviceId')
    const { operation } = await enqueueAccess({ clientId: id, deviceId, kind: 'delete', actor: actorOf(user), key: accessRequestKey(event) })
    return accepted(event, { operation })
  }),
  'POST /clients/:id/devices/:deviceId/reissue': admin(async (event, user) => {
    const { id, deviceId } = recordIds(event, 'id', 'deviceId')
    const { operation } = await enqueueAccess({ clientId: id, deviceId, kind: 'reissue', actor: actorOf(user), key: accessRequestKey(event) })
    return accepted(event, { operation })
  }),

  'GET /clients/:id/devices/:deviceId/wg-config': admin(async event => text(event, await panelWg(event))),
  'GET /clients/:id/devices/:deviceId/wg-qrcode.png': admin(async event => typed(event, 'image/png', qrPng(await panelWg(event)))),
  'GET /clients/:id/devices/:deviceId/wg-qrcode.svg': admin(async event => typed(event, 'image/svg+xml; charset=utf-8', qrSvg(await panelWg(event)))),
  'GET /clients/:id/devices/:deviceId/ovpn-config': admin(async event =>
    text(event, (await panelConfig(event, () => requireOvpnEndpoint(), ovpnConfig)).conf)),
  /** JSON с server/username/password/remoteId + CA cert (PEM): эти поля UI выводит копируемыми. */
  'GET /clients/:id/devices/:deviceId/ikev2-credentials': admin(async event =>
    buildIkev2ClientInfo((await panelDevice(event, () => requireIkev2())).device.id)),

  // Отправка в админский чат Telegram. Секрет бота - до поиска в базе: без бота 503 и для чужого id.
  'POST /clients/:id/devices/:deviceId/send-wg-to-tg': admin(async (event) => {
    const { client, device, conf } = await panelConfig(event, () => {
      const host = requireWgEndpoint()
      botSecretOrThrow()
      return host
    }, wgConfig)
    await sendBotEvent(wgBotEvent(deviceConfigName(client.name, device.name, device.id), conf))
    return { ok: true }
  }),
  // Секрет бота проверяется до готовности CA: без бота 503 bot_not_configured при любом CA.
  'POST /clients/:id/devices/:deviceId/send-ovpn-to-tg': admin(async (event) => {
    const { client, device, conf } = await panelConfig(event, () => requireOvpnEndpoint(botSecretOrThrow), ovpnConfig)
    await sendBotEvent({ type: 'client_send_openvpn', name: deviceConfigName(client.name, device.name, device.id), conf })
    return { ok: true }
  }),
  /**
   * IKEv2-доступ двумя сообщениями: текст с server/login/password и ca.crt файлом.
   * Платформы не различаем — у нативного IKEv2 на всех ОС одинаковый набор
   * полей и одинаковый CA. Сами .mobileconfig/.sswan не делаем.
   */
  'POST /clients/:id/devices/:deviceId/send-ikev2-to-tg': admin(async (event) => {
    const { client, device } = await panelDevice(event, () => requireIkev2(botSecretOrThrow))
    const info = await buildIkev2ClientInfo(device.id)
    await sendBotEvent({
      type: 'client_send_ikev2',
      clientName: client.name,
      username: info.username,
      server: info.server,
      password: info.password,
      caCertPem: info.caCertPem,
    })
    return { ok: true as const }
  }),

  /**
   * Скачать серверный CA как PEM. Нужен только в self-signed-режиме.
   * В letsencrypt-режиме — 404: LE-корень уже в trust-store клиентов.
   */
  'GET /ikev2/ca.crt': admin(async (event) => {
    if ((await ikev2Mode()) === 'letsencrypt') {
      throw createError({
        statusCode: 404,
        statusMessage: 'ca_not_needed', message: 'CA не нужен в letsencrypt-режиме (корневой Let\'s Encrypt уже в trust-store iOS/macOS/Windows/Android)',
      })
    }
    if (!(await ikev2CaReady())) {
      throw createError({ statusCode: 503, statusMessage: 'ikev2_ca_not_ready', message: 'IKEv2 CA ещё не инициализирован (стадия 27-ikev2)' })
    }
    const pem = await readIkev2CaPem()
    setHeader(event, 'content-disposition', 'attachment; filename="anysda-ikev2-ca.crt"')
    return typed(event, 'application/x-pem-file', pem)
  }),

  // Клиент ведёт свои девайсы через бота.
  'POST /bot/devices': bot(async (event) => {
    const { chatId, name } = await readBodyAs(event, z.object({ chatId: ChatId, name: Name }))
    const { client } = await addDevice(event, name, { source: 'bot', chatId, name }, () => clientByChat(chatId), {
      exists: 'Девайс с таким названием уже есть',
      limit: () => 'Достигнут лимит устройств — обратитесь к администратору',
    }, (fresh) => {
      // Администратор мог отозвать бота, пока шли await.
      if (fresh.tgChatId !== chatId) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
    })
    return botClientView(client.id)
  }),
  'DELETE /bot/devices/:deviceId': bot(event => botAccess(event, 'delete', () => Number(getQuery(event).chatId))),
  'POST /bot/devices/:deviceId/reissue': bot(event =>
    botAccess(event, 'reissue', async () => (await readBodyAs(event, z.object({ chatId: ChatId }))).chatId)),
  'GET /bot/devices/:deviceId/wg-config': bot(async event => text(event, await botConfig(event, requireWgEndpoint, wgConfig))),
  // QR для бота: тот же генератор, что SVG панели.
  'GET /bot/devices/:deviceId/wg-qrcode.png': bot(async event => typed(event, 'image/png', qrPng(await botConfig(event, requireWgEndpoint, wgConfig)))),
  'GET /bot/devices/:deviceId/ovpn-config': bot(async event => text(event, await botConfig(event, () => requireOvpnEndpoint(), ovpnConfig))),
}
