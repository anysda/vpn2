import { createHash, X509Certificate } from 'node:crypto'
import { and, eq, exists, isNull } from 'drizzle-orm'
import { claimUnique, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'

// openssl ca не блокирует index.txt/serial/crlnumber: выдача, перевыпуск и отзыв идут по очереди.
let caQueue: Promise<unknown> = Promise.resolve()
function withCaLock<T>(fn: () => Promise<T>): Promise<T> {
  const run = caQueue.then(fn)
  caQueue = run.catch(() => {})
  return run
}

const db = () => useDb()
async function deviceRow(deviceId: number) {
  const [row] = await db().select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)
  return row
}
/** Владелец ещё на той же версии доступа и не удаляется. */
const ownerAt = (id: number, version: number) => exists(db().select({ id: clientsTable.id }).from(clientsTable)
  .where(and(eq(clientsTable.id, id), eq(clientsTable.accessVersion, version), eq(clientsTable.deleting, false))))

/** Постоянный адрес OpenVPN устройства. */
function ensureDeviceOvpnIp(deviceId: number) {
  return claimUnique(`device ${deviceId}: не удалось занять адрес OpenVPN`, async () => {
    const row = await deviceRow(deviceId)
    if (row.ovpnIp) return row
    const ip = nextPoolAddress('ovpn', (await db().select({ ip: devicesTable.ovpnIp }).from(devicesTable)).map(r => r.ip))
    const [updated] = await db().update(devicesTable).set({ ovpnIp: ip, updatedAt: new Date() })
      .where(and(eq(devicesTable.id, deviceId), isNull(devicesTable.ovpnIp))).returning()
    return updated
  })
}

export const ovpnCn = (deviceId: number): string => `dev_${deviceId}`

/** Недоступный сервис - ошибка, а не выключенный CA. */
export async function caReady(): Promise<boolean> {
  return (await control<{ ready: boolean }>('profile.read', { protocol: 'openvpn' })).ready
}

function issueClientCert(deviceId: number, previous = ''): Promise<{ cert: string, key: string }> {
  return control('openvpn.issue', { device_id: deviceId, generation: createHash('sha256').update(previous).digest('hex') })
}

/** Номер устройства и серийный номер управляемого сертификата `CN=dev_<id>`. */
export function certificateOf(certPem: string): { device_id: number, serial: string } {
  const cert = new X509Certificate(certPem)
  const match = cert.subject.match(/(?:^|\n)CN=dev_(\d+)(?:\n|$)/)
  if (!match) throw new Error('Invalid managed certificate')
  return { device_id: Number(match[1]), serial: cert.serialNumber }
}

export function revokeAccessCert(certPem: string): Promise<void> {
  const { device_id, serial } = certificateOf(certPem)
  return control('openvpn.revoke', { device_id, serial })
}

export async function killOvpnClient(cn: string): Promise<boolean> {
  const match = /^dev_(\d+)$/.exec(cn)
  if (!match) throw new Error('Invalid managed device')
  return (await control<{ killed: boolean }>('openvpn.disconnect', { device_id: Number(match[1]) })).killed
}

async function writeCcd(deviceId: number, ip: string | null, disabled: boolean, expiresAt: Date | null): Promise<void> {
  if (!ip) throw new Error('OpenVPN address missing')
  await controlApply('openvpn.apply', { device_id: deviceId, ip, action: disabled ? 'deny' : 'allow', expires_at: expiryForControl(expiresAt) })
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

const inline = (tag: string, body: string) => [`<${tag}>`, body.trim(), `</${tag}>`]

/** Единый .ovpn со встроенными ca, cert, key и tls-crypt. */
export async function buildOvpnConfig(p: OvpnConfigParams): Promise<string> {
  // Семейство remote выбирает клиент; суффикс 4/6 и -server относятся только к серверу.
  if (!/^(?:udp[46]?|tcp[46]?(?:-server)?)$/.test(p.proto)) throw new Error(`unsupported OpenVPN protocol: ${p.proto}`)
  const udp = p.proto.startsWith('udp')
  const { ca, tlsCrypt } = await control<{ ca: string, tlsCrypt: string }>('profile.read', { protocol: 'openvpn' })
  return [
    'client', 'dev tun', `proto ${udp ? 'udp' : 'tcp-client'}`, `remote ${p.serverHost} ${p.serverPort}`,
    'resolv-retry infinite', 'nobind', 'persist-key', 'persist-tun',
    // Туннель только IPv4: весь IPv6 заводим в tun и блокируем, иначе он утекает мимо VPN.
    'block-ipv6', 'route-ipv6 ::/1', 'route-ipv6 8000::/1',
    'remote-cert-tls server',
    // Системные буферы сокета и запись в UDP без лишнего poll.
    'sndbuf 0', 'rcvbuf 0', ...(udp ? ['fast-io'] : []),
    'cipher AES-256-GCM', 'auth SHA256', 'verb 3',
    ...p.dns.map(d => `dhcp-option DNS ${d}`),
    ...ovpnLocalRoutes(p.splitLocal ?? true, p.tunnelPrefixes ?? ['10.99.0.']),
    '',
    ...inline('ca', ca), ...inline('cert', p.clientCert), ...inline('key', p.clientKey), ...inline('tls-crypt', tlsCrypt),
    '',
  ].join('\n')
}

/** Выдаёт устройству сертификат OpenVPN, если его нет; адрес и ccd - до выдачи конфига. */
export function ensureDeviceOvpn(deviceId: number) {
  return withCaLock(async () => {
    const row = await deviceRow(deviceId)
    if (row.ovpnCert && row.ovpnKey && row.ovpnIp) return row
    if (!row.ovpnCert || !row.ovpnKey) {
      const [owner] = await db().select().from(clientsTable).where(eq(clientsTable.id, row.clientId))
      if (!owner || owner.deleting || row.deleting || !isClientActive(owner)) {
        throw createError({ statusCode: 409, message: 'Выдача сертификата недоступна до включения доступа' })
      }
      const { cert, key } = await issueClientCert(deviceId)
      const [issued] = await db().update(devicesTable).set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
        .where(and(eq(devicesTable.id, deviceId), eq(devicesTable.deleting, false), ownerAt(owner.id, owner.accessVersion))).returning()
      if (!issued) throw new Error(`device ${deviceId} not found`)
    }
    const updated = await ensureDeviceOvpnIp(deviceId)
    const [client] = await db().select().from(clientsTable).where(eq(clientsTable.id, row.clientId)).limit(1)
    await writeCcd(deviceId, updated.ovpnIp, updated.deleting || !client || !isClientActive(client), client?.expiresAt ?? null)
    return updated
  })
}

/** Перевыпуск: старый сертификат отзывается в CRL и его живая сессия рвётся (CRL проверяется только при подключении). */
export function reissueDeviceOvpn(deviceId: number, clientId: number, version: number) {
  return withCaLock(async () => {
    try {
      const [owner] = await db().select({ id: clientsTable.id }).from(clientsTable)
        .where(and(eq(clientsTable.id, clientId), eq(clientsTable.accessVersion, version)))
      if (!owner) throw new Error('superseded')
      const row = await deviceRow(deviceId)
      if (row.ovpnCert) {
        await revokeAccessCert(row.ovpnCert)
        await killOvpnClient(ovpnCn(deviceId))
      }
      if (!row.ovpnCert && !(await caReady())) return row
      const { cert, key } = await issueClientCert(deviceId, row.ovpnCert ?? '')
      const [updated] = await db().update(devicesTable).set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
        .where(and(eq(devicesTable.id, deviceId), ownerAt(clientId, version))).returning()
      if (!updated) throw new Error(`device ${deviceId} not found`)
      return updated
    }
    catch {
      useLogger().error({ deviceId }, 'ovpn reissue failed')
      throw createError({ statusCode: 503, statusMessage: 'ovpn_reissue_failed', message: 'Ошибка перевыпуска' })
    }
  })
}

/** Сверка ccd с базой для каждого устройства с сертификатом; подключённых без записи в базе выбиваем. */
export function syncOpenvpnConfig(): Promise<void> {
  return withCaLock(async () => {
    if (!useRuntimeConfig().ovpnEnabled) return
    if (!(await caReady())) throw new Error('OpenVPN CA unavailable')
    const rows = await db().select({
      id: devicesTable.id, ovpnCert: devicesTable.ovpnCert, ovpnIp: devicesTable.ovpnIp, deleting: devicesTable.deleting,
      frozenManual: clientsTable.frozenManual, expiresAt: clientsTable.expiresAt,
    }).from(devicesTable).innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
    const pinned = new Set<string>()
    for (const r of rows.filter(r => r.ovpnCert)) {
      await writeCcd(r.id, r.ovpnIp ?? (await ensureDeviceOvpnIp(r.id)).ovpnIp, r.deleting || !isClientActive(r), r.expiresAt)
      pinned.add(ovpnCn(r.id))
    }
    const { sessions } = await control<{ sessions: Record<string, string> }>('state.read', { protocol: 'openvpn' })
    for (const cn of Object.keys(sessions)) {
      if (/^dev_\d+$/.test(cn) && !pinned.has(cn)) await killOvpnClient(cn)
    }
  })
}
