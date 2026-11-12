import { generateKeyPairSync, randomBytes } from 'node:crypto'
import { isIP } from 'node:net'
import os from 'node:os'
import { and, eq, isNull } from 'drizzle-orm'
import { claimUnique, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { coalesceRuns } from './api'

/**
 * IPv6 endpoint попал бы в split-local 2000::/3, поэтому берём IPv4 входа, а без него
 * единственный публичный IPv4 интерфейсов сервера (не туннель, docker или LAN). Домены сохраняем.
 */
function wgEndpoint(host: string): string {
  if (isIP(host.trim().replace(/^\[(.*)\]$/, '$1')) !== 6) return host
  const entryIp = (process.env.NUXT_WG_ENDPOINT_IP || '').trim()
  if (isIP(entryIp) === 4) return entryIp
  const addresses = new Set(Object.values(os.networkInterfaces()).flatMap(records =>
    (records || []).filter(r => !r.internal && r.family === 'IPv4' && !isPrivateAddress(r.address)).map(r => r.address)))
  if (addresses.size === 1) return [...addresses][0]!
  throw createError({
    statusCode: 422, statusMessage: 'wg_ipv4_endpoint_unavailable',
    message: 'Не удалось определить IPv4-адрес сервера для WireGuard. Укажите доступный клиентам IPv4 сервера в entry.public_host и примените конфигурацию.',
  })
}

/** Пара ключей WireGuard: сырые 32 байта X25519 из хвоста DER, в base64. */
export function wgKeypair(): { privateKey: string, publicKey: string } {
  const { privateKey, publicKey } = generateKeyPairSync('x25519')
  return {
    privateKey: (privateKey.export({ format: 'der', type: 'pkcs8' }) as Buffer).subarray(-32).toString('base64'),
    publicKey: (publicKey.export({ format: 'der', type: 'spki' }) as Buffer).subarray(-32).toString('base64'),
  }
}

/** ULA-адрес клиента с тем же последним октетом, что у v4: стеки по RFC 6724 предпочтут v4. Первая /24 без номера подсети. */
function wgUlaAddress(wgIp: string): string {
  const m = /^\d{1,3}\.\d{1,3}\.(\d{1,3})\.(\d{1,3})$/.exec(wgIp)
  if (!m) throw new Error(`wgUlaAddress: не разобран v4-адрес "${wgIp}"`)
  return wgIp.startsWith('10.66.66.') ? `fd66:66::${m[2]}` : `fd66:66::${m[1]}:${m[2]}`
}

export interface WgClientConfigParams {
  clientPrivateKey: string
  clientPresharedKey: string
  clientIp: string
  serverPublicKey: string
  serverEndpoint: string
  serverPort: number
  dns?: string
  mtu?: number
  /** true (по умолчанию): локальные сети клиента идут мимо туннеля, см. `wgAllowedIps`. */
  splitLocal?: boolean
  /** Сети, которые всё-таки идут в туннель: wg-сегмент и mgmt (DNS). */
  tunnelPrefixes?: string[]
}

export function buildWgClientConfig(p: WgClientConfigParams): string {
  const endpoint = wgEndpoint(p.serverEndpoint)
  // Endpoint бывает именем: из AllowedIPs вычитается IP входа.
  const allowed = wgAllowedIps(p.splitLocal ?? true, p.tunnelPrefixes ?? ['10.66.66.', '10.99.0.'], endpoint, process.env.NUXT_WG_ENDPOINT_IP || '')
  return [
    '[Interface]',
    `PrivateKey = ${p.clientPrivateKey}`,
    `Address = ${p.clientIp}/24, ${wgUlaAddress(p.clientIp)}/64`,
    `DNS = ${p.dns ?? '10.99.0.1'}`,
    `MTU = ${p.mtu ?? 1420}`,
    '',
    '[Peer]',
    `PublicKey = ${p.serverPublicKey}`,
    `PresharedKey = ${p.clientPresharedKey}`,
    `Endpoint = ${endpoint}:${p.serverPort}`,
    `AllowedIPs = ${allowed}`,
    'PersistentKeepalive = 25',
    '',
  ].join('\n')
}

export function loadServerKeys(): Promise<{ publicKey: string }> {
  return control('profile.read', { protocol: 'wg' })
}

/** Выдаёт устройству ключи и адрес WG, если их нет; запись условная, чтобы параллельный вызов не перетёр выданное. */
export function ensureDeviceWg(deviceId: number) {
  const db = useDb()
  return claimUnique(`device ${deviceId}: не удалось занять адрес WG`, async () => {
    const row = await deviceById(deviceId)
    if (row.wgPrivateKey && row.wgPublicKey && row.wgPresharedKey && row.wgIp) return row
    const kp = wgKeypair()
    const ip = row.wgIp ?? nextPoolAddress('wg', (await db.select({ ip: devicesTable.wgIp }).from(devicesTable)).map(r => r.ip))
    const [updated] = await db.update(devicesTable).set({
      wgPrivateKey: row.wgPrivateKey ?? kp.privateKey,
      wgPublicKey: row.wgPublicKey ?? kp.publicKey,
      wgPresharedKey: row.wgPresharedKey ?? randomBytes(32).toString('base64'),
      wgIp: ip,
      updatedAt: new Date(),
    }).where(and(eq(devicesTable.id, deviceId), row.wgIp ? eq(devicesTable.wgIp, row.wgIp) : isNull(devicesTable.wgIp))).returning()
    return updated
  })
}

/** Серверный конфиг WG из всех устройств; пир есть только у активного клиента. */
export const syncWireguardConfig = coalesceRuns(async () => {
  if (!useRuntimeConfig().wgEnabled) return
  const rows = await useDb().select({
    wgPublicKey: devicesTable.wgPublicKey, wgPresharedKey: devicesTable.wgPresharedKey, wgIp: devicesTable.wgIp, deleting: devicesTable.deleting,
    frozenManual: clientsTable.frozenManual, expiresAt: clientsTable.expiresAt,
  }).from(devicesTable).innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
  const peers = rows.filter(r => r.wgPublicKey && r.wgPresharedKey && r.wgIp && !r.deleting && isClientActive(r))
    .map(r => ({ key: r.wgPublicKey!, preshared_key: r.wgPresharedKey!, ip: r.wgIp!, expires_at: expiryForControl(r.expiresAt) }))
  await controlApply('wg.apply', { peers })
})
