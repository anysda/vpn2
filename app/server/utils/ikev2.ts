import { randomBytes } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { slugify } from './naming'

const exec = promisify(execFile)

// strongSwan конфиги/ключи (стадия 27-ikev2 их раскатывает на entry).
// Панель работает с этими путями из своего контейнера (bind-mount через
// 30-frontend, аналогично /etc/openvpn). SS_CONF (/etc/swanctl/conf.d/anysda.conf)
// добавим в этап 2 когда появится render-логика.
const SS_PKI_DIR = '/etc/strongswan/pki'
const CA_CERT = `${SS_PKI_DIR}/ca.crt`

// IKEv2 IP-пул (per-device static). .1 — gateway (anysda на entry).
const IKEV2_SUBNET = '10.68.68.'

/**
 * Сгенерировать 20-символьный alnum-пароль (как у clients.password). Не используем
 * никакие спецсимволы — пароль вводится руками в нативный VPN-клиент iOS/Win.
 */
export function generateIkev2Password(): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  const bytes = randomBytes(20)
  let out = ''
  for (let i = 0; i < 20; i++) out += alphabet[bytes[i] % alphabet.length]
  return out
}

/**
 * Подобрать свободный /32 в IKEv2-сабнете. .1 — gateway, клиенты с .2.
 * Логика идентична nextAvailableWgIp в wireguard.ts.
 */
export function nextAvailableIkev2Ip(usedIps: Array<string | null | undefined>): string {
  const used = new Set(usedIps.filter(Boolean) as string[])
  for (let i = 2; i <= 254; i++) {
    const ip = `${IKEV2_SUBNET}${i}`
    if (!used.has(ip)) return ip
  }
  throw new Error('IKEv2 subnet exhausted (10.68.68.0/24)')
}

/**
 * Username = «<клиент-слаг>-<девайс-слаг>», уникальный среди ВСЕХ устройств
 * (не только этого клиента) — иначе swanctl secrets конфликтнут. При коллизии
 * добавляем числовой суффикс «-2», «-3», и т.д.
 */
export function buildIkev2Username(
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

/** True если стадия 27-ikev2 уже раскатала server CA. Иначе issue/sync no-op. */
export async function ikev2CaReady(): Promise<boolean> {
  try {
    await fs.access(CA_CERT)
    return true
  }
  catch {
    return false
  }
}

/** Чтение PEM серверного CA (для вшивания в .mobileconfig и выдачи отдельно). */
export async function readIkev2CaPem(): Promise<string> {
  return fs.readFile(CA_CERT, 'utf8')
}

/**
 * Убедиться что у устройства есть IKEv2-креды. Если нет — генерим и пишем в БД.
 * Возвращает (возможно обновлённую) запись device. Безопасно вызывать многократно.
 */
export async function ensureDeviceIkev2(deviceId: number) {
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
  const ip = row.ikev2Ip ?? nextAvailableIkev2Ip(all.map(r => r.ip))

  const [updated] = await db
    .update(devicesTable)
    .set({
      ikev2Username: username,
      ikev2Password: password,
      ikev2Ip: ip,
      updatedAt: new Date(),
    })
    .where(eq(devicesTable.id, deviceId))
    .returning()
  return updated
}

/**
 * Перевыпуск IKEv2-пароля устройства. Username и IP сохраняются.
 * Активные SA дропаются через `swanctl --terminate ike-id=<username>` (no-op
 * если swanctl недоступен — стадия 27 ещё не отработала).
 */
export async function reissueDeviceIkev2(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)

  // Если кредов ещё нет — просто выдадим первый набор.
  if (!row.ikev2Username) return ensureDeviceIkev2(deviceId)

  const newPassword = generateIkev2Password()
  const [updated] = await db
    .update(devicesTable)
    .set({ ikev2Password: newPassword, updatedAt: new Date() })
    .where(eq(devicesTable.id, deviceId))
    .returning()

  // Терминируем активную SA — клиенту придётся переподключиться с новым паролем.
  await terminateIkev2Sa(row.ikev2Username).catch((err) => {
    useLogger().warn(
      { err: (err as Error).message, deviceId },
      'ikev2 terminate-sa skipped (swanctl unavailable?)',
    )
  })
  return updated
}

/**
 * Атомарная переписка /etc/swanctl/conf.d/anysda.conf из БД + swanctl --load-*.
 * Заглушка для этапа 1 — рендер conf.d вынесем на этап 2 (когда swanctl будет
 * установлен на entry). Сейчас просто no-op если CA нет.
 */
export async function syncIkev2(): Promise<void> {
  if (!(await ikev2CaReady())) return
  // TODO (этап 2-3): рендер /etc/swanctl/conf.d/anysda.conf из БД,
  //                  swanctl --load-creds --load-pools --load-conns.
  useLogger().debug('syncIkev2: stub (этап 1) — conf rendering выйдет в этапе 2')
}

/** swanctl --terminate ike-id=<username>: рвёт активную SA конкретного устройства. */
export async function terminateIkev2Sa(username: string): Promise<void> {
  if (!(await ikev2CaReady())) return
  await exec('swanctl', ['--terminate', '--ike-id', username], { timeout: 10_000 })
}

/**
 * Структура «инструкция для клиента»: то что показывается в UI/боте.
 * server/username/password — копируемые поля; caCertPem — отдельно
 * для импорта в Trusted Root на Windows/Linux; mobileconfig — для iOS.
 */
export interface Ikev2ClientInfo {
  server: string
  remoteId: string  // тот же IP (в iOS поле Remote ID)
  username: string
  password: string
  caCertPem: string | null
}

/** Собрать поля для отображения клиенту. */
export async function buildIkev2ClientInfo(deviceId: number, serverIp: string): Promise<Ikev2ClientInfo> {
  const device = await ensureDeviceIkev2(deviceId)
  const ca = (await ikev2CaReady()) ? await readIkev2CaPem() : null
  return {
    server: serverIp,
    remoteId: serverIp,
    username: device.ikev2Username!,
    password: device.ikev2Password!,
    caCertPem: ca,
  }
}

/**
 * Apple .mobileconfig (XML) с зашитыми Server/Username/Password + CA cert.
 * Импорт в один тык в iOS/macOS, без ручного ввода.
 * Реализация — этап 4 (нужны Settings → VPN payload UUID-ы и base64 CA).
 */
export async function renderClientMobileconfig(_deviceId: number, _serverIp: string): Promise<string> {
  throw new Error('renderClientMobileconfig: not implemented yet (этап 4)')
}

/** strongSwan конфиг сервера ещё не разворачивается на этапе 1 — заглушка. */
export async function ensureIkev2Server(): Promise<void> {
  // Этап 2: PKI bootstrap + swanctl conn. Сейчас no-op.
}
