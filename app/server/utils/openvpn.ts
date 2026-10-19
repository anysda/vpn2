import { createHash, X509Certificate } from 'node:crypto'
import { control, controlApply } from './control'
import { and, eq, exists, isNull } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { ovpnLocalRoutes } from './allowed-ips'
import { expiryForControl, isClientActive } from './client-status'
import { nextPoolAddress } from './address-pools'

// openssl ca не блокирует общие index.txt/serial/crlnumber. Выдачу,
// перевыпуск и отзыв выполняем по очереди, включая чтение/запись устройства:
// два запроса одного конфига не должны выпускать два разных сертификата.
let caQueue: Promise<unknown> = Promise.resolve()

function withCaLock<T>(fn: () => Promise<T>): Promise<T> {
  const run = caQueue.then(fn)
  caQueue = run.catch(() => {})
  return run
}

/**
 * Выдать устройству постоянный адрес OpenVPN, если его нет. Повтор при
 * конфликте UNIQUE — та же гонка выбора свободного адреса, что у WG и IKEv2.
 */
async function ensureDeviceOvpnIp(deviceId: number) {
  const db = useDb()
  for (let attempt = 1; ; attempt++) {
    const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
    if (!row) throw new Error(`device ${deviceId} not found`)
    if (row.ovpnIp) return row
    const all = await db.select({ ip: devicesTable.ovpnIp }).from(devicesTable)
    try {
      const [updated] = await db
        .update(devicesTable)
        .set({ ovpnIp: nextPoolAddress('ovpn', all.map(r => r.ip)), updatedAt: new Date() })
        .where(and(eq(devicesTable.id, deviceId), isNull(devicesTable.ovpnIp)))
        .returning()
      if (updated) return updated
    }
    catch (err) {
      if (!isUniqueViolation(err)) throw err
    }
    if (attempt >= IP_CLAIM_ATTEMPTS) throw new Error(`device ${deviceId}: не удалось занять адрес OpenVPN`)
  }
}

/** OpenVPN cert CN for a device — stable, derived from the DB device id. */
export function ovpnCn(deviceId: number): string {
  return `dev_${deviceId}`
}

/** Readiness errors propagate: an unreachable service is not a disabled CA. */
export async function caReady(): Promise<boolean> {
  return (await control<{ ready: boolean }>('profile.read', { protocol: 'openvpn' })).ready
}

async function issueClientCert(deviceId: number, previous = ''): Promise<{ cert: string, key: string }> {
  return control('openvpn.issue', { device_id: deviceId, generation: createHash('sha256').update(previous).digest('hex') })
}

export function revokeAccessCert(certPem: string): Promise<void> {
  const cert = new X509Certificate(certPem)
  const match = cert.subject.match(/(?:^|\n)CN=dev_(\d+)(?:\n|$)/)
  if (!match) throw new Error('Invalid managed certificate')
  return control('openvpn.revoke', { device_id: Number(match[1]), serial: cert.serialNumber })
}

export async function killOvpnClient(cn: string): Promise<boolean> {
  const match = /^dev_(\d+)$/.exec(cn)
  if (!match) throw new Error('Invalid managed device')
  return (await control<{ killed: boolean }>('openvpn.disconnect', { device_id: Number(match[1]) })).killed
}

async function connectedOvpnClients(): Promise<Map<string, string>> {
  const { sessions } = await control<{ sessions: Record<string, string> }>('state.read', { protocol: 'openvpn' })
  return new Map(Object.entries(sessions))
}

async function killOvpnClientQuiet(cn: string): Promise<void> {
  await killOvpnClient(cn)
}

async function writeCcd(cn: string, ip: string | null, disabled: boolean, expiresAt: Date | null): Promise<void> {
  if (!ip) throw new Error('OpenVPN address missing')
  await controlApply('openvpn.apply', { device_id: Number(cn.slice(4)), ip, action: disabled ? 'deny' : 'allow', expires_at: expiryForControl(expiresAt) })
}

export interface OvpnConfigParams {
  clientCert: string
  clientKey: string
  serverHost: string
  serverPort: number
  proto: string
  dns: string[]
  /** true (по умолчанию) — локалка клиента остаётся мимо туннеля. */
  splitLocal?: boolean
  /** Сети, которые всё-таки должны идти В туннель: mgmt (DNS AdGuard). */
  tunnelPrefixes?: string[]
}

/** Render a unified .ovpn with inline ca / cert / key / tls-crypt blocks. */
export async function buildOvpnConfig(p: OvpnConfigParams): Promise<string> {
  // Семейство remote выбирает клиент (IPv4, IPv6 или DNS с A/AAAA).
  // Суффикс 4/6 ограничивает bind сервера, а -server задаёт роль TCP;
  // перенос этих ограничений в профиль ломает dual-stack и смену адреса.
  if (!/^(?:udp[46]?|tcp[46]?(?:-server)?)$/.test(p.proto)) {
    throw new Error(`unsupported OpenVPN protocol: ${p.proto}`)
  }
  const clientProto = p.proto.startsWith('udp') ? 'udp' : 'tcp-client'
  const { ca, tlsCrypt } = await control<{ ca: string, tlsCrypt: string }>('profile.read', { protocol: 'openvpn' })
  return [
    `client`,
    `dev tun`,
    `proto ${clientProto}`,
    `remote ${p.serverHost} ${p.serverPort}`,
    `resolv-retry infinite`,
    `nobind`,
    `persist-key`,
    `persist-tun`,
    // Туннель только IPv4 (tun0 10.67.67.0/24). Загоняем весь IPv6 в tun
    // (route-ipv6 ::/1 + 8000::/1) и отбиваем его block-ipv6 — иначе IPv6
    // клиента уходит мимо туннеля напрямую (leak), а приложения падают на
    // IPv4, который идёт через VPN.
    `block-ipv6`,
    `route-ipv6 ::/1`,
    `route-ipv6 8000::/1`,
    `remote-cert-tls server`,
    // Буферы сокета системные, а не мелкие встроенные; fast-io - запись в
    // UDP без лишнего poll. OpenVPN Connect эти опции молча пропускает.
    `sndbuf 0`,
    `rcvbuf 0`,
    ...(p.proto.startsWith('udp') ? ['fast-io'] : []),
    `cipher AES-256-GCM`,
    `auth SHA256`,
    `verb 3`,
    ...p.dns.map(d => `dhcp-option DNS ${d}`),
    ...ovpnLocalRoutes(p.splitLocal ?? true, p.tunnelPrefixes ?? ['10.99.0.']),
    ``,
    `<ca>`,
    ca.trim(),
    `</ca>`,
    `<cert>`,
    p.clientCert.trim(),
    `</cert>`,
    `<key>`,
    p.clientKey.trim(),
    `</key>`,
    `<tls-crypt>`,
    tlsCrypt.trim(),
    `</tls-crypt>`,
    ``,
  ].join('\n')
}

/**
 * Make sure the DEVICE has an OpenVPN cert. If absent, issue one and persist
 * the cert + key on the device row. Returns the (possibly updated) record.
 */
export function ensureDeviceOvpn(deviceId: number) {
  return withCaLock(() => ensureDeviceOvpnOnce(deviceId))
}

async function ensureDeviceOvpnOnce(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)
  if (row.ovpnCert && row.ovpnKey && row.ovpnIp) return row

  if (!row.ovpnCert || !row.ovpnKey) {
    const [owner] = await db.select().from(clientsTable).where(eq(clientsTable.id, row.clientId))
    if (!owner || owner.deleting || row.deleting || !isClientActive(owner)) {
      throw createError({ statusCode: 409, message: 'Выдача сертификата недоступна до включения доступа' })
    }
    const { cert, key } = await issueClientCert(deviceId)
    const [issued] = await db
      .update(devicesTable)
      .set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
      .where(and(eq(devicesTable.id, deviceId), eq(devicesTable.deleting, false), exists(
        db.select({ id: clientsTable.id }).from(clientsTable).where(and(
          eq(clientsTable.id, owner.id), eq(clientsTable.accessVersion, owner.accessVersion), eq(clientsTable.deleting, false),
        )),
      )))
      .returning()
    if (!issued) throw new Error(`device ${deviceId} not found`)
  }
  // Адрес и ccd — до того, как клиент получит конфиг: первое же подключение
  // идёт с постоянным адресом.
  const updated = await ensureDeviceOvpnIp(deviceId)
  const [client] = await db
    .select({ frozenManual: clientsTable.frozenManual, expiresAt: clientsTable.expiresAt })
    .from(clientsTable)
    .where(eq(clientsTable.id, row.clientId))
    .limit(1)
  await writeCcd(ovpnCn(deviceId), updated.ovpnIp, updated.deleting || (client ? !isClientActive(client) : true), client?.expiresAt ?? null)
  return updated
}

/** Перевыпуск OVPN-сертификата девайса: старый отзывается в CRL, выдаётся новый. */
export function reissueDeviceOvpn(deviceId: number, clientId: number, version: number) {
  return withCaLock(async () => {
    try {
      return await reissueDeviceOvpnOnce(deviceId, clientId, version)
    }
    catch {
      useLogger().error({ deviceId }, 'ovpn reissue failed')
      throw createError({ statusCode: 503, statusMessage: 'ovpn_reissue_failed', message: 'Ошибка перевыпуска' })
    }
  })
}

async function reissueDeviceOvpnOnce(deviceId: number, clientId: number, version: number) {
  const db = useDb()
  const [owner] = await db.select({ id: clientsTable.id }).from(clientsTable)
    .where(and(eq(clientsTable.id, clientId), eq(clientsTable.accessVersion, version)))
  if (!owner) throw new Error('superseded')
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)

  // Неготовая CA не отменяет отзыв: до публикации CRL и успешной выдачи
  // нового сертификата сохраняем старую пару в БД для повтора.
  if (row.ovpnCert) {
    await revokeAccessCert(row.ovpnCert)
    // CRL проверяется только при подключении — живую сессию со старым
    // сертификатом рвём сами.
    await killOvpnClient(ovpnCn(deviceId))
  }
  // Без ранее выданного сертификата OpenVPN остаётся необязательным.
  if (!row.ovpnCert && !(await caReady())) return row

  const { cert, key } = await issueClientCert(deviceId, row.ovpnCert ?? '')
  const [updated] = await db
    .update(devicesTable)
    .set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
    .where(and(eq(devicesTable.id, deviceId), exists(db.select({ id: clientsTable.id }).from(clientsTable)
      .where(and(eq(clientsTable.id, clientId), eq(clientsTable.accessVersion, version))))))
    .returning()
  if (!updated) throw new Error(`device ${deviceId} not found`)
  return updated
}

/**
 * Сверка ccd с базой для всех устройств с сертификатом: постоянный адрес
 * (устройствам до VPN2-115 выдаётся здесь), флаг disable, удаление ccd
 * исчезнувших устройств. Подключённых замороженных и тех, кто сидит не на
 * своём адресе (подключился до записи ccd), выбиваем: и то и другое OpenVPN
 * применяет только при новом подключении.
 */
export async function syncOpenvpnConfig(): Promise<void> {
  return withCaLock(syncOpenvpnConfigOnce)
}

async function syncOpenvpnConfigOnce(): Promise<void> {
  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) return
  if (!(await caReady())) throw new Error('OpenVPN CA unavailable')

  const db = useDb()
  const rows = await db
    .select({
      id: devicesTable.id,
      ovpnCert: devicesTable.ovpnCert,
      ovpnIp: devicesTable.ovpnIp,
      deleting: devicesTable.deleting,
      frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))

  for (const r of rows) {
    if (!r.ovpnCert) continue
    const ip = r.ovpnIp ?? (await ensureDeviceOvpnIp(r.id)).ovpnIp
    await writeCcd(ovpnCn(r.id), ip, r.deleting || !isClientActive(r), r.expiresAt)
  }
  const pinned = new Set(rows.filter(r => r.ovpnCert).map(r => ovpnCn(r.id)))
  for (const [cn] of await connectedOvpnClients()) {
    if (/^dev_\d+$/.test(cn) && !pinned.has(cn)) await killOvpnClientQuiet(cn)
  }
}
