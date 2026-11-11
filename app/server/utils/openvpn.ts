import { createHash, X509Certificate } from 'node:crypto'
import { control, controlApply } from './control'
import { and, eq, exists, isNull } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { ovpnLocalRoutes } from './allowed-ips'
import { expiryForControl, isClientActive } from './client-status'
import { nextPoolAddress } from './address-pools'

// openssl ca не блокирует index.txt/serial/crlnumber: выдача, перевыпуск и отзыв идут по очереди.
let caQueue: Promise<unknown> = Promise.resolve()

function withCaLock<T>(fn: () => Promise<T>): Promise<T> {
  const run = caQueue.then(fn)
  caQueue = run.catch(() => {})
  return run
}

/** Выдаёт устройству постоянный адрес OpenVPN, если его нет. Конфликт UNIQUE от параллельной выдачи повторяется. */
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

export const ovpnCn = (deviceId: number): string => `dev_${deviceId}`

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
  /** true (по умолчанию) - локальная сеть клиента остаётся мимо туннеля. */
  splitLocal?: boolean
  /** Сети, которые идут в туннель: mgmt с DNS AdGuard. */
  tunnelPrefixes?: string[]
}

/** Единый .ovpn со встроенными ca, cert, key и tls-crypt. */
export async function buildOvpnConfig(p: OvpnConfigParams): Promise<string> {
  // Семейство remote выбирает клиент; суффикс 4/6 и -server относятся только к серверу.
  if (!/^(?:udp[46]?|tcp[46]?(?:-server)?)$/.test(p.proto)) throw new Error(`unsupported OpenVPN protocol: ${p.proto}`)
  const { ca, tlsCrypt } = await control<{ ca: string, tlsCrypt: string }>('profile.read', { protocol: 'openvpn' })
  return [
    `client`,
    `dev tun`,
    `proto ${p.proto.startsWith('udp') ? 'udp' : 'tcp-client'}`,
    `remote ${p.serverHost} ${p.serverPort}`,
    `resolv-retry infinite`,
    `nobind`,
    `persist-key`,
    `persist-tun`,
    // Туннель только IPv4: весь IPv6 заводим в tun и блокируем, иначе он утекает мимо VPN.
    `block-ipv6`,
    `route-ipv6 ::/1`,
    `route-ipv6 8000::/1`,
    `remote-cert-tls server`,
    // Системные буферы сокета и запись в UDP без лишнего poll.
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

/** Выдаёт устройству сертификат OpenVPN, если его нет, и возвращает запись. */
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
  // Адрес и ccd до выдачи конфига: первое же подключение идёт с постоянным адресом.
  const updated = await ensureDeviceOvpnIp(deviceId)
  const [client] = await db.select().from(clientsTable).where(eq(clientsTable.id, row.clientId)).limit(1)
  await writeCcd(ovpnCn(deviceId), updated.ovpnIp, updated.deleting || (client ? !isClientActive(client) : true), client?.expiresAt ?? null)
  return updated
}

/** Перевыпуск сертификата устройства: старый отзывается в CRL, выдаётся новый. */
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

  // Неготовая CA не отменяет отзыв. CRL проверяется только при подключении,
  // поэтому живую сессию со старым сертификатом рвём сами.
  if (row.ovpnCert) {
    await revokeAccessCert(row.ovpnCert)
    await killOvpnClient(ovpnCn(deviceId))
  }
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
 * Сверка ccd с базой: постоянный адрес и запрет для каждого устройства с
 * сертификатом. Подключённых без записи в базе выбиваем.
 */
export async function syncOpenvpnConfig(): Promise<void> {
  return withCaLock(syncOpenvpnConfigOnce)
}

async function syncOpenvpnConfigOnce(): Promise<void> {
  if (!useRuntimeConfig().ovpnEnabled) return
  if (!(await caReady())) throw new Error('OpenVPN CA unavailable')

  const rows = await useDb()
    .select({
      id: devicesTable.id, ovpnCert: devicesTable.ovpnCert, ovpnIp: devicesTable.ovpnIp, deleting: devicesTable.deleting,
      frozenManual: clientsTable.frozenManual, expiresAt: clientsTable.expiresAt,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
  const pinned = new Set<string>()
  for (const r of rows) {
    if (!r.ovpnCert) continue
    const ip = r.ovpnIp ?? (await ensureDeviceOvpnIp(r.id)).ovpnIp
    await writeCcd(ovpnCn(r.id), ip, r.deleting || !isClientActive(r), r.expiresAt)
    pinned.add(ovpnCn(r.id))
  }
  const { sessions } = await control<{ sessions: Record<string, string> }>('state.read', { protocol: 'openvpn' })
  for (const cn of Object.keys(sessions)) {
    if (/^dev_\d+$/.test(cn) && !pinned.has(cn)) await killOvpnClient(cn)
  }
}
