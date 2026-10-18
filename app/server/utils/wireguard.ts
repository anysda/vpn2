import { generateKeyPairSync, randomBytes } from 'node:crypto'
import { control, controlApply } from './control'
import { and, eq, isNull } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { wgAllowedIps } from './allowed-ips'
import { isClientActive } from './client-status'
import { coalesceRuns } from './coalesce'
import { nextPoolAddress } from './address-pools'
import { wgEndpoint } from './wg-endpoint'


/**
 * WireGuard keypair. Uses Node's native X25519 — no `wg` binary required.
 * Both fields are 32-byte raw keys encoded as base64.
 */
function generateWgKeypair(): { privateKey: string, publicKey: string } {
  const { privateKey, publicKey } = generateKeyPairSync('x25519')
  // X25519 raw key sits at the tail of the DER export (last 32 bytes).
  const priv = (privateKey.export({ format: 'der', type: 'pkcs8' }) as Buffer).subarray(-32)
  const pub = (publicKey.export({ format: 'der', type: 'spki' }) as Buffer).subarray(-32)
  return { privateKey: priv.toString('base64'), publicKey: pub.toString('base64') }
}

function generateWgPresharedKey(): string {
  return randomBytes(32).toString('base64')
}

/**
 * ULA-адрес клиента в туннеле, парный к его `10.66.66.N`. Один и тот же
 * последний октет на обоих концах, единственный ULA-источник у хаба, чтобы
 * стеки Apple/Android по RFC 6724 сами предпочли v4 живому пиру.
 */
function wgUlaAddress(wgIp: string): string {
  const m = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.(\d{1,3})$/.exec(wgIp)
  if (!m) throw new Error(`wgUlaAddress: не разобран v4-адрес "${wgIp}"`)
  // Сохраняем ULA старой /24 побайтно. Новые /24 получают отдельное поле,
  // иначе .2 из разных подсетей претендовали бы на один AllowedIPs /128.
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
  /**
   * true (по умолчанию) — локальные сети клиента остаются мимо туннеля,
   * см. `wgAllowedIps`. false — классический full-tunnel 0.0.0.0/0.
   */
  splitLocal?: boolean
  /** Сети, которые всё-таки должны идти В туннель: wg-сегмент и mgmt (DNS). */
  tunnelPrefixes?: string[]
}

/** Render a client-facing wg-quick .conf string. */
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

/** Only the public server key crosses the privilege boundary. */
export async function loadServerKeys(): Promise<{ publicKey: string }> {
  return control('profile.read', { protocol: 'wg' })
}

/**
 * Ensure the given DEVICE has WG keys + an IP. If missing, fill them in and
 * persist. Returns the (possibly newly-populated) device record.
 *
 * Адрес выбирается по снимку занятых, а между снимком и записью параллельный
 * запрос может занять тот же. Защита — UNIQUE на devices.wg_ip: проигравший
 * получает конфликт и берёт следующий свободный (VPN2-50). Запись условная
 * (адрес у строки ещё тот, что мы прочли), чтобы параллельный вызов для того
 * же устройства не перетёр уже выданные ключи.
 */
export async function ensureDeviceWg(deviceId: number) {
  const db = useDb()
  for (let attempt = 1; ; attempt++) {
    const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
    if (!row) throw new Error(`device ${deviceId} not found`)

    if (row.wgPrivateKey && row.wgPublicKey && row.wgPresharedKey && row.wgIp) return row

    const all = await db.select({ ip: devicesTable.wgIp }).from(devicesTable)
    const kp = generateWgKeypair()
    const psk = generateWgPresharedKey()
    const ip = row.wgIp ?? nextPoolAddress('wg', all.map(r => r.ip))

    try {
      const [updated] = await db
        .update(devicesTable)
        .set({
          wgPrivateKey: row.wgPrivateKey ?? kp.privateKey,
          wgPublicKey: row.wgPublicKey ?? kp.publicKey,
          wgPresharedKey: row.wgPresharedKey ?? psk,
          wgIp: ip,
          updatedAt: new Date(),
        })
        .where(and(
          eq(devicesTable.id, deviceId),
          row.wgIp ? eq(devicesTable.wgIp, row.wgIp) : isNull(devicesTable.wgIp),
        ))
        .returning()
      if (updated) return updated
      // Строку успел дописать параллельный вызов — перечитываем.
    }
    catch (err) {
      if (!isUniqueViolation(err)) throw err
    }
    if (attempt >= IP_CLAIM_ATTEMPTS) throw new Error(`device ${deviceId}: не удалось занять адрес WG`)
  }
}


/**
 * Render the WG server config from ALL devices and apply it via `wg syncconf`.
 * A device's peer is included only if its client is active (not frozen and
 * not expired). Call after any change touching devices or client status.
 */
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
    .map(r => ({ key: r.wgPublicKey!, preshared_key: r.wgPresharedKey!, ip: r.wgIp! }))
  await controlApply('wg.apply', { peers })
}

/** Параллельные вызовы (создание устройств пачкой) идут по очереди, см. coalesceRuns. */
export const syncWireguardConfig = coalesceRuns(syncWireguardConfigOnce)
