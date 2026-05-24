import { randomBytes } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { isClientActive } from './client-status'
import { slugify } from './naming'

const exec = promisify(execFile)

// strongSwan/swanctl пути. Стадия 27-ikev2 раскатывает CA + базовый conf;
// панель добавляет динамику в anysda-clients.conf (eap secrets из БД).
// Bind-mount в контейнер панели — см. infra/scripts/30-frontend.sh.
const SS_PKI_DIR = '/etc/strongswan/pki'
const CA_CERT = `${SS_PKI_DIR}/ca.crt`
const SS_CLIENTS_CONF = '/etc/swanctl/conf.d/anysda-clients.conf'

// Маркеры режима (стадия 27 пишет в /etc/anysda/, оно bind-mount'нуто).
const MODE_FILE   = '/etc/anysda/ikev2-mode'         // letsencrypt | self-signed
const HOST_FILE   = '/etc/anysda/ikev2-server-host'  // <domain> или <ip entry>

/**
 * Режим IKEv2-сервера: 'letsencrypt' если PANEL_DOMAIN задан И Caddy выдал
 * cert, иначе 'self-signed'. В LE-режиме клиенту НЕ нужен CA (LE-корень
 * уже в trust-store iOS/macOS/Win/Android).
 */
export async function ikev2Mode(): Promise<'letsencrypt' | 'self-signed'> {
  try {
    const s = (await fs.readFile(MODE_FILE, 'utf8')).trim()
    return s === 'letsencrypt' ? 'letsencrypt' : 'self-signed'
  }
  catch {
    return 'self-signed'
  }
}

/**
 * Server-address который клиент впишет в поле «Server» VPN-настроек. В LE —
 * domain (panel.domain), в self-signed — IP entry. Стадия 27 записывает в
 * /etc/anysda/ikev2-server-host; fallback: ENTRY_HOST env, wgPublicHost cfg.
 */
export async function ikev2ServerHost(): Promise<string> {
  try {
    const s = (await fs.readFile(HOST_FILE, 'utf8')).trim()
    if (s) return s
  }
  catch { /* fall through */ }
  const cfg = useRuntimeConfig()
  return String(process.env.ENTRY_HOST || cfg.wgPublicHost || '').trim()
}

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
 * Атомарная переписка /etc/swanctl/conf.d/anysda-clients.conf из БД +
 * `swanctl --load-creds` (только credentials меняются, conns/pools статичны
 * из стадии 27-ikev2). Включаются только активные клиенты (frozen/expired
 * не попадают в secrets — попытка логина отбивается EAP-фейлом).
 *
 * NB: терминацию активной SA при изменении (reissue/delete/freeze) делает
 * вызывающий код через terminateIkev2Sa(username); syncIkev2 только пишет
 * актуальный secrets-блок и перегружает credentials.
 */
export async function syncIkev2(): Promise<void> {
  if (!(await ikev2CaReady())) return

  const db = useDb()
  const rows = await db
    .select({
      username: devicesTable.ikev2Username,
      password: devicesTable.ikev2Password,
      ip: devicesTable.ikev2Ip,
      frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))

  const active = rows.filter(r =>
    r.username && r.password && r.ip
    && isClientActive({ frozenManual: r.frozenManual, expiresAt: r.expiresAt }),
  )

  // Каждое устройство — отдельная секция eap-XXX в secrets {} (имя секции
  // не несёт смысла; charon матчит по `id = <username>`).
  const lines: string[] = [
    '# Managed by anysda-vpn2 panel — НЕ редактировать вручную.',
    '# secrets для EAP-MSCHAPv2 клиентов IKEv2. Перегенерируется на каждое',
    '# изменение клиента/устройства (server/utils/ikev2.ts → syncIkev2()).',
    '',
    'secrets {',
  ]
  for (const r of active) {
    // shell-escape не нужен — swanctl format «id = ...» / «secret = "..."»
    // экранирует двойные кавычки и обратные слеши.
    const safePass = r.password!.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
    const safeUser = r.username!.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
    lines.push(
      `  eap-${r.username} {`,
      `    id = "${safeUser}"`,
      `    secret = "${safePass}"`,
      `  }`,
    )
  }
  lines.push('}', '')

  const body = lines.join('\n')
  const tmp = `${SS_CLIENTS_CONF}.tmp-${process.pid}`
  await fs.mkdir(path.dirname(SS_CLIENTS_CONF), { recursive: true })
  await fs.writeFile(tmp, body, { mode: 0o600 })
  await fs.rename(tmp, SS_CLIENTS_CONF)

  // Перегружаем credentials. conns/pools статичны (из 27-ikev2.sh) — не трогаем.
  await exec('swanctl', ['--load-creds'], { timeout: 10_000 }).catch((err) => {
    useLogger().warn(
      { err: (err as Error).message },
      'ikev2 swanctl --load-creds skipped (binary missing / charon down?)',
    )
  })
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

/**
 * Собрать поля для отображения клиенту. server = ikev2ServerHost() (domain
 * в LE-режиме, IP в self-signed). caCertPem = null в LE-режиме — клиенту
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
  return {
    server: host,
    remoteId: host,
    username: device.ikev2Username!,
    password: device.ikev2Password!,
    caCertPem: ca,
  }
}

// .mobileconfig-рендер удалён намеренно — UX упрощён до одного TG-сообщения
// + ca.crt файла (как у WG: один файл, никаких xml/UUID/Apple-payload'ов).
// Клиент вводит server/login/password руками в нативный IKEv2-клиент.

/** strongSwan конфиг сервера ещё не разворачивается на этапе 1 — заглушка. */
export async function ensureIkev2Server(): Promise<void> {
  // Этап 2: PKI bootstrap + swanctl conn. Сейчас no-op.
}
