import { generateKeyPairSync, randomBytes } from 'node:crypto'
import { isIP } from 'node:net'
import os from 'node:os'
import { createError } from 'h3'
import { control, controlApply } from './control'
import { and, eq, isNull } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { wgAllowedIps } from './allowed-ips'
import { expiryForControl, isClientActive } from './client-status'
import { coalesceRuns } from './coalesce'
import { nextPoolAddress } from './address-pools'

/** Только публичный IPv4: адрес туннеля, docker или LAN не годится для автоопределения. */
function isPublicIpv4(address: string): boolean {
  if (isIP(address) !== 4) return false
  const [a, b] = address.split('.').map(Number) as [number, number, number, number]
  return a !== 0 && a !== 10 && a !== 127 && a < 224
    && !(a === 100 && b >= 64 && b <= 127)
    && !(a === 169 && b === 254)
    && !(a === 172 && b >= 16 && b <= 31)
    && !(a === 192 && b === 168)
}

/**
 * IPv6 endpoint попал бы в split-local 2000::/3, поэтому берём IPv4 входа, а без
 * него единственный публичный IPv4 интерфейсов сервера. Домены сохраняем.
 */
function wgEndpoint(host: string): string {
  const literal = host.trim().replace(/^\[(.*)\]$/, '$1')
  if (isIP(literal) !== 6) return host

  const entryIp = (process.env.NUXT_WG_ENDPOINT_IP || '').trim()
  if (isIP(entryIp) === 4) return entryIp

  const addresses = new Set(Object.values(os.networkInterfaces()).flatMap(records =>
    (records || []).filter(r => !r.internal && r.family === 'IPv4' && isPublicIpv4(r.address))
      .map(r => r.address),
  ))
  if (addresses.size === 1) return [...addresses][0]!

  throw createError({
    statusCode: 422,
    statusMessage: 'wg_ipv4_endpoint_unavailable',
    message: 'Не удалось определить IPv4-адрес сервера для WireGuard. Укажите доступный клиентам IPv4 сервера в entry.public_host и примените конфигурацию.',
  })
}

/** Пара ключей WireGuard: сырые 32 байта X25519 из хвоста DER, в base64. */
function generateWgKeypair(): { privateKey: string, publicKey: string } {
  const { privateKey, publicKey } = generateKeyPairSync('x25519')
  const priv = (privateKey.export({ format: 'der', type: 'pkcs8' }) as Buffer).subarray(-32)
  const pub = (publicKey.export({ format: 'der', type: 'spki' }) as Buffer).subarray(-32)
  return { privateKey: priv.toString('base64'), publicKey: pub.toString('base64') }
}

/** ULA-адрес клиента, парный к его v4: тот же последний октет, чтобы стеки по RFC 6724 предпочли v4. */
function wgUlaAddress(wgIp: string): string {
  const m = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.(\d{1,3})$/.exec(wgIp)
  if (!m) throw new Error(`wgUlaAddress: не разобран v4-адрес "${wgIp}"`)
  // Старая /24 сохраняет прежний ULA, новые получают номер подсети отдельным полем.
  const subnet = wgIp.split('.')[2]
  return wgIp.startsWith('10.66.66.') ? `fd66:66::${m[1]}` : `fd66:66::${subnet}:${m[1]}`
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
  const dns = p.dns ?? '10.99.0.1'
  const mtu = p.mtu ?? 1420
  const endpoint = wgEndpoint(p.serverEndpoint)
  const allowed = wgAllowedIps(
    p.splitLocal ?? true,
    p.tunnelPrefixes ?? ['10.66.66.', '10.99.0.'],
    endpoint,
    // IP входа из стадии 30-frontend: Endpoint бывает именем (public_host).
    process.env.NUXT_WG_ENDPOINT_IP || '',
  )
  return [
    `[Interface]`,
    `PrivateKey = ${p.clientPrivateKey}`,
    `Address = ${p.clientIp}/24, ${wgUlaAddress(p.clientIp)}/64`,
    `DNS = ${dns}`,
    `MTU = ${mtu}`,
    ``,
    `[Peer]`,
    `PublicKey = ${p.serverPublicKey}`,
    `PresharedKey = ${p.clientPresharedKey}`,
    `Endpoint = ${endpoint}:${p.serverPort}`,
    `AllowedIPs = ${allowed}`,
    `PersistentKeepalive = 25`,
    ``,
  ].join('\n')
}

export async function loadServerKeys(): Promise<{ publicKey: string }> {
  return control('profile.read', { protocol: 'wg' })
}

/**
 * Выдаёт устройству ключи и адрес WG, если их нет. От гонки за адрес защищает
 * UNIQUE на devices.wg_ip: проигравший берёт следующий (VPN2-50); запись
 * условная, чтобы параллельный вызов не перетёр выданные ключи.
 */
export async function ensureDeviceWg(deviceId: number) {
  const db = useDb()
  for (let attempt = 1; ; attempt++) {
    const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
    if (!row) throw new Error(`device ${deviceId} not found`)

    if (row.wgPrivateKey && row.wgPublicKey && row.wgPresharedKey && row.wgIp) return row

    const all = await db.select({ ip: devicesTable.wgIp }).from(devicesTable)
    const kp = generateWgKeypair()
    const ip = row.wgIp ?? nextPoolAddress('wg', all.map(r => r.ip))

    try {
      const [updated] = await db
        .update(devicesTable)
        .set({
          wgPrivateKey: row.wgPrivateKey ?? kp.privateKey,
          wgPublicKey: row.wgPublicKey ?? kp.publicKey,
          wgPresharedKey: row.wgPresharedKey ?? randomBytes(32).toString('base64'),
          wgIp: ip,
          updatedAt: new Date(),
        })
        .where(and(
          eq(devicesTable.id, deviceId),
          row.wgIp ? eq(devicesTable.wgIp, row.wgIp) : isNull(devicesTable.wgIp),
        ))
        .returning()
      if (updated) return updated
    }
    catch (err) {
      if (!isUniqueViolation(err)) throw err
    }
    if (attempt >= IP_CLAIM_ATTEMPTS) throw new Error(`device ${deviceId}: не удалось занять адрес WG`)
  }
}
/** Серверный конфиг WG из всех устройств; пир есть только у активного клиента. */
async function syncWireguardConfigOnce(): Promise<void> {
  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) return

  const db = useDb()
  const rows = await db
    .select({
      wgPublicKey: devicesTable.wgPublicKey,
      wgPresharedKey: devicesTable.wgPresharedKey,
      wgIp: devicesTable.wgIp,
      deleting: devicesTable.deleting,
      frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))

  const peers = rows
    .filter(r => r.wgPublicKey && r.wgPresharedKey && r.wgIp && !r.deleting && isClientActive(r))
    .map(r => ({ key: r.wgPublicKey!, preshared_key: r.wgPresharedKey!, ip: r.wgIp!, expires_at: expiryForControl(r.expiresAt) }))
  await controlApply('wg.apply', { peers })
}

export const syncWireguardConfig = coalesceRuns(syncWireguardConfigOnce)
