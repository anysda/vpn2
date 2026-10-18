import { randomBytes } from 'node:crypto'
import { control, controlApply } from './control'
import { and, eq, isNotNull, isNull, or } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { isClientActive } from './client-status'
import { slugify } from './naming'
import { coalesceRuns } from './coalesce'
import { nextPoolAddress } from './address-pools'
import { clientDns } from './client-dns'


interface Ikev2Profile { ready: boolean, mode: 'letsencrypt' | 'self-signed', host: string, domain: string, ca: string }
function profile(): Promise<Ikev2Profile> { return control('profile.read', { protocol: 'ikev2' }) }

export async function ikev2Mode(): Promise<'letsencrypt' | 'self-signed'> {
  return (await profile()).mode
}

async function ikev2ServerHost(): Promise<string> {
  return (await profile()).host || String(process.env.ENTRY_HOST || useRuntimeConfig().wgPublicHost || '').trim()
}

/**
 * Сгенерировать 20-символьный alnum-пароль (как у clients.password). Не используем
 * никакие спецсимволы — пароль вводится руками в нативный VPN-клиент iOS/Win.
 */
function generateIkev2Password(): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  let out = ''
  for (const b of randomBytes(20)) out += alphabet.charAt(b % alphabet.length)
  return out
}

/**
 * Username = «<клиент-слаг>-<девайс-слаг>», уникальный среди ВСЕХ устройств
 * (не только этого клиента) — иначе swanctl secrets конфликтнут. При коллизии
 * добавляем числовой суффикс «-2», «-3», и т.д.
 */
function buildIkev2Username(
  clientName: string,
  deviceName: string,
  takenUsernames: Array<string | null | undefined>,
): string {
  const base = `${slugify(clientName)}-${slugify(deviceName)}`
  const taken = new Set(takenUsernames.filter(Boolean) as string[])
  if (!taken.has(base)) return base
  for (let i = 2; i < 1000; i++) {
    const candidate = `${base}-${i}`
    if (!taken.has(candidate)) return candidate
  }
  throw new Error(`unable to allocate unique ikev2 username for ${base}`)
}

export async function ikev2ServerReady(): Promise<boolean> { return (await profile()).ready }
export async function ikev2CaReady(): Promise<boolean> { return Boolean((await profile()).ca) }
export async function readIkev2CaPem(): Promise<string> { return (await profile()).ca }

/**
 * Убедиться что у устройства есть IKEv2-креды. Если нет — генерим и пишем в БД.
 * Возвращает (возможно обновлённую) запись device. Безопасно вызывать многократно.
 */
export async function ensureDeviceIkev2(deviceId: number) {
  // Повтор при конфликте UNIQUE на ikev2_ip или ikev2_username — та же гонка
  // выбора свободного значения, что и у адреса WG (VPN2-50, VPN2-70), см.
  // ensureDeviceWg. На повторе занятые перечитываются заново.
  for (let attempt = 1; ; attempt++) {
    try {
      const updated = await claimDeviceIkev2(deviceId)
      if (updated) return updated
    }
    catch (err) {
      if (!isUniqueViolation(err)) throw err
    }
    if (attempt >= IP_CLAIM_ATTEMPTS) throw new Error(`device ${deviceId}: не удалось занять логин и адрес IKEv2`)
  }
}

/** Одна попытка выдать креды. undefined — строку успел дописать параллельный вызов. */
async function claimDeviceIkev2(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)
  if (row.ikev2Username && row.ikev2Password && row.ikev2Ip) return row

  const [client] = await db
    .select({ name: clientsTable.name })
    .from(clientsTable)
    .where(eq(clientsTable.id, row.clientId))
    .limit(1)
  if (!client) throw new Error(`client ${row.clientId} not found`)

  // Подтягиваем все занятые usernames и IP — генерим уникальные.
  const all = await db
    .select({ username: devicesTable.ikev2Username, ip: devicesTable.ikev2Ip })
    .from(devicesTable)

  const username = row.ikev2Username
    ?? buildIkev2Username(client.name, row.name, all.map(r => r.username))
  const password = row.ikev2Password ?? generateIkev2Password()
  const ip = row.ikev2Ip ?? nextPoolAddress('ikev2', all.map(r => r.ip))

  const [updated] = await db
    .update(devicesTable)
    .set({
      ikev2Username: username,
      ikev2Password: password,
      ikev2Ip: ip,
      updatedAt: new Date(),
    })
    .where(and(
      eq(devicesTable.id, deviceId),
      row.ikev2Ip ? eq(devicesTable.ikev2Ip, row.ikev2Ip) : isNull(devicesTable.ikev2Ip),
      row.ikev2Username ? eq(devicesTable.ikev2Username, row.ikev2Username) : isNull(devicesTable.ikev2Username),
    ))
    .returning()
  return updated
}


/**
 * Атомарная переписка /etc/swanctl/conf.d/anysda-clients.conf из БД.
 * Включаются только активные клиенты (frozen/expired не попадают в файл —
 * попытка логина отбивается).
 *
 * Постоянный адрес устройства (VPN2-115). Нативные клиенты iOS, macOS и
 * Windows шлют IKE-идентификатором свой IP, имя приходит только по
 * EAP-Identity, поэтому выбрать conn по имени заранее нельзя. Общая conn
 * стадии 27 (eap_id = %any) спрашивает имя и проверяет пароль, но здесь ей
 * дописано требование группы, которой ни у кого нет. После EAP charon ищет
 * другую подходящую conn и берёт conn устройства с этим eap_id, а её пул —
 * один постоянный адрес устройства. conn и пул устройства наследуют всё
 * остальное от общих (`имя : connections.anysda-ikev2`), swanctl сливает
 * разделы из всех файлов conf.d. Старая стадия 27 с этим файлом и новая
 * стадия 27 со старым файлом работают как раньше, с общим пулом.
 *
 * Применение и разрыв устаревших SA выполняет хостовый anysda-ikev2-sync.
 * Очередь подтверждает операцию только по свежему ответу применителя.
 */
async function syncIkev2Once(): Promise<void> {
  if (!useRuntimeConfig().ikev2Enabled) return

  const db = useDb()

  // Креды выданы не целиком: адрес сняла миграция 0010 с устройства-дубля
  // (VPN2-50), логин и пароль — миграция 0012 (VPN2-70). Выдаём недостающее
  // здесь, а не ждём, пока кто-то откроет креды: адрес назначает сервер, а
  // дублю логина без нового логина вход всё равно не светит.
  const orphans = await db
    .select({ id: devicesTable.id })
    .from(devicesTable)
    .where(or(
      and(isNotNull(devicesTable.ikev2Username), isNull(devicesTable.ikev2Ip)),
      and(isNull(devicesTable.ikev2Username), isNotNull(devicesTable.ikev2Ip)),
    ))
  if (orphans.length) {
    // Массовое восстановление не должно перечитывать весь пул для каждого
    // устройства (2000 × 2000 строк блокировали event loop). Снимок и выдача
    // в одной write-транзакции: параллельный POST не займёт выбранные значения.
    await db.transaction(async (tx) => {
      const all = await tx.select({
        id: devicesTable.id, name: devicesTable.name, clientName: clientsTable.name,
        username: devicesTable.ikev2Username, password: devicesTable.ikev2Password,
        ip: devicesTable.ikev2Ip,
      }).from(devicesTable).innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
      const usernames = all.map(r => r.username)
      const ips = all.map(r => r.ip)
      for (const row of all) {
        if (Boolean(row.username) === Boolean(row.ip)) continue
        let ip: string
        let username: string
        try {
          ip = row.ip ?? nextPoolAddress('ikev2', ips)
          username = row.username ?? buildIkev2Username(row.clientName, row.name, usernames)
        }
        catch (err) {
          useLogger().warn({ err: (err as Error).message, id: row.id }, 'ikev2: адрес устройству не выдан')
          continue
        }
        await tx.update(devicesTable).set({
          ikev2Username: username, ikev2Ip: ip,
          ikev2Password: row.password ?? generateIkev2Password(), updatedAt: new Date(),
        }).where(eq(devicesTable.id, row.id))
        if (!row.username) usernames.push(username)
        if (!row.ip) ips.push(ip)
      }
    }, { behavior: 'immediate' })
  }

  const rows = await db
    .select({
      id: devicesTable.id,
      username: devicesTable.ikev2Username,
      deleting: devicesTable.deleting,
      password: devicesTable.ikev2Password,
      ip: devicesTable.ikev2Ip,
      frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt,
      filterTraffic: clientsTable.filterTraffic,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))

  const active = rows.filter(r =>
    !r.deleting && r.username && r.password && r.ip
    && isClientActive({ frozenManual: r.frozenManual, expiresAt: r.expiresAt }),
  )

  await controlApply('ikev2.apply', {
    clients: active.map(r => ({ device_id: r.id, username: r.username!, password: r.password!, ip: r.ip!, dns: clientDns(r.filterTraffic) })),
  })
}

/** Параллельные вызовы (создание устройств пачкой) идут по очереди, см. coalesceRuns. */
export const syncIkev2 = coalesceRuns(syncIkev2Once)


/**
 * Структура «инструкция для клиента»: то что показывается в UI/боте.
 * server/username/password — копируемые поля; caCertPem — отдельно
 * для импорта в Trusted Root на Windows/Linux; mobileconfig — для iOS.
 */
export interface Ikev2ClientInfo {
  server: string
  remoteId: string  // тот же IP или домен (в iOS поле Remote ID)
  username: string
  password: string
  caCertPem: string | null
  certificatePending: boolean
}

/**
 * Собрать поля для отображения клиенту. server = ikev2ServerHost() (domain
 * в LE-режиме, адрес WG/OVPN в self-signed). caCertPem = null в LE-режиме: клиенту
 * НЕ нужен анысда-CA, LE-корень уже в trust-store.
 *
 * Параметр `_serverIp` оставлен для обратной совместимости вызывающего кода;
 * фактический host теперь резолвится из /etc/anysda/ikev2-server-host
 * (стадия 27 ставит) — это даёт consistency между сервером и панелью.
 */
export async function buildIkev2ClientInfo(deviceId: number, _serverIp: string): Promise<Ikev2ClientInfo> {
  const device = await ensureDeviceIkev2(deviceId)
  const [mode, host] = await Promise.all([ikev2Mode(), ikev2ServerHost()])
  const ca = mode === 'letsencrypt'
    ? null
    : ((await ikev2CaReady()) ? await readIkev2CaPem() : null)
  const domain = (await profile()).domain
  return {
    server: host,
    remoteId: host,
    username: device.ikev2Username!,
    password: device.ikev2Password!,
    caCertPem: ca,
    certificatePending: Boolean(domain.trim() && (mode !== 'letsencrypt' || host !== domain.trim())),
  }
}

// .mobileconfig-рендер удалён намеренно — UX упрощён до одного TG-сообщения
// + ca.crt файла (как у WG: один файл, никаких xml/UUID/Apple-payload'ов).
// Клиент вводит server/login/password руками в нативный IKEv2-клиент.
