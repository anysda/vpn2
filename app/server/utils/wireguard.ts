import { generateKeyPairSync, randomBytes } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { wgAllowedIps } from './allowed-ips'
import { isClientActive } from './client-status'

const exec = promisify(execFile)

/**
 * WireGuard keypair. Uses Node's native X25519 — no `wg` binary required.
 * Both fields are 32-byte raw keys encoded as base64.
 */
export function generateWgKeypair(): { privateKey: string, publicKey: string } {
  const { privateKey, publicKey } = generateKeyPairSync('x25519')
  // X25519 raw key sits at the tail of the DER export (last 32 bytes).
  const priv = (privateKey.export({ format: 'der', type: 'pkcs8' }) as Buffer).subarray(-32)
  const pub = (publicKey.export({ format: 'der', type: 'spki' }) as Buffer).subarray(-32)
  return { privateKey: priv.toString('base64'), publicKey: pub.toString('base64') }
}

export function generateWgPresharedKey(): string {
  return randomBytes(32).toString('base64')
}

/**
 * ULA-адрес клиента в туннеле, парный к его `10.66.66.N`. Один и тот же
 * последний октет на обоих концах, единственный ULA-источник у хаба, чтобы
 * стеки Apple/Android по RFC 6724 сами предпочли v4 живому пиру.
 */
export function wgUlaAddress(wgIp: string): string {
  const m = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.(\d{1,3})$/.exec(wgIp)
  return `fd66:66::${m ? m[1] : '1'}`
}

/**
 * Pick the next free /32 inside the WG subnet (default 10.66.66.0/24).
 * .1 is the server, clients start at .2.
 */
export function nextAvailableWgIp(usedIps: Array<string | null | undefined>, subnetPrefix = '10.66.66.'): string {
  const used = new Set(usedIps.filter(Boolean) as string[])
  for (let i = 2; i <= 254; i++) {
    const ip = `${subnetPrefix}${i}`
    if (!used.has(ip)) return ip
  }
  throw new Error('WireGuard subnet exhausted (10.66.66.0/24)')
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
  const allowed = wgAllowedIps(
    p.splitLocal ?? true,
    p.tunnelPrefixes ?? ['10.66.66.', '10.99.0.'],
    p.serverEndpoint,
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
    `Endpoint = ${p.serverEndpoint}:${p.serverPort}`,
    `AllowedIPs = ${allowed}`,
    `PersistentKeepalive = 25`,
    ``,
  ].join('\n')
}

interface ServerConfigParams {
  serverPrivateKey: string
  listenPort: number
  serverIp: string
  peers: Array<{
    publicKey: string
    presharedKey: string
    allowedIp: string  // single /32
    enabled: boolean
  }>
}

/**
 * Atomically rewrite /etc/wireguard/wg0.conf and run `wg syncconf` so the
 * kernel interface picks up the new peer list without dropping existing
 * sessions. Skipped (with a log warning) if wireguard-tools isn't available.
 */
export async function writeWgServerConfig(
  p: ServerConfigParams,
  confPath = '/etc/wireguard/wg0.conf',
): Promise<void> {
  const serverUla = wgUlaAddress(p.serverIp)
  const lines: string[] = [
    `# Managed by anysda-vpn2 panel. Do not edit by hand.`,
    `[Interface]`,
    `Address = ${p.serverIp}/24, ${serverUla}/64`,
    `ListenPort = ${p.listenPort}`,
    `PrivateKey = ${p.serverPrivateKey}`,
    ``,
  ]
  for (const peer of p.peers) {
    if (!peer.enabled) continue
    lines.push(
      `[Peer]`,
      `PublicKey = ${peer.publicKey}`,
      `PresharedKey = ${peer.presharedKey}`,
      `AllowedIPs = ${peer.allowedIp}/32, ${wgUlaAddress(peer.allowedIp)}/128`,
      ``,
    )
  }
  const body = lines.join('\n')

  const tmp = `${confPath}.tmp-${process.pid}`
  await fs.mkdir(path.dirname(confPath), { recursive: true })
  await fs.writeFile(tmp, body, { mode: 0o600 })
  await fs.rename(tmp, confPath)

  try {
    // `wg-quick strip` emits only [Interface] + [Peer] (no PostUp/PostDown
    // hooks), which is exactly what `wg syncconf` accepts. `syncconf` does
    // NOT touch interface addresses, поэтому ULA на wg0 ставим отдельно:
    // `replace` идемпотентен и не рвёт существующие сессии пиров.
    await exec('bash', ['-c', `wg-quick strip wg0 | wg syncconf wg0 /dev/stdin`], { timeout: 10_000 })
    await exec('ip', ['-6', 'addr', 'replace', `${serverUla}/64`, 'dev', 'wg0'], { timeout: 5_000 })
  }
  catch (err) {
    // wg0 may not exist (cold start before 28-wireguard ran) or wg binary
    // missing (local dev). Don't fail the API call — the next stage-28 run
    // will pick the conf up from disk.
    useLogger().warn({ err: (err as Error).message }, 'wg syncconf skipped')
  }
}

const SERVER_PRIVKEY_PATH = '/etc/wireguard/server.priv'
const SERVER_PUBKEY_PATH = '/etc/wireguard/server.pub'

/**
 * Load the server's WG keypair from /etc/wireguard. The keys are seeded by
 * stage 28 and read by the panel for peer-config rendering.
 */
export async function loadServerKeys(): Promise<{ privateKey: string, publicKey: string }> {
  const [priv, pub] = await Promise.all([
    fs.readFile(SERVER_PRIVKEY_PATH, 'utf8').then(s => s.trim()).catch(() => ''),
    fs.readFile(SERVER_PUBKEY_PATH, 'utf8').then(s => s.trim()).catch(() => ''),
  ])
  if (!priv || !pub) throw new Error('WireGuard server keys not initialised — run stage 28-wireguard')
  return { privateKey: priv, publicKey: pub }
}

/**
 * Ensure the given DEVICE has WG keys + an IP. If missing, fill them in and
 * persist. Returns the (possibly newly-populated) device record.
 */
export async function ensureDeviceWg(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)

  if (row.wgPrivateKey && row.wgPublicKey && row.wgPresharedKey && row.wgIp) return row

  const all = await db.select({ ip: devicesTable.wgIp }).from(devicesTable)
  const kp = generateWgKeypair()
  const psk = generateWgPresharedKey()
  const ip = nextAvailableWgIp(all.map(r => r.ip))

  const [updated] = await db
    .update(devicesTable)
    .set({
      wgPrivateKey: row.wgPrivateKey ?? kp.privateKey,
      wgPublicKey: row.wgPublicKey ?? kp.publicKey,
      wgPresharedKey: row.wgPresharedKey ?? psk,
      wgIp: row.wgIp ?? ip,
      updatedAt: new Date(),
    })
    .where(eq(devicesTable.id, deviceId))
    .returning()
  return updated
}

/** Перевыпуск WG-ключей девайса: новый keypair + PSK, IP сохраняется. */
export async function reissueDeviceWg(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)

  const kp = generateWgKeypair()
  const psk = generateWgPresharedKey()
  let ip = row.wgIp
  if (!ip) {
    const all = await db.select({ ip: devicesTable.wgIp }).from(devicesTable)
    ip = nextAvailableWgIp(all.map(r => r.ip))
  }

  const [updated] = await db
    .update(devicesTable)
    .set({
      wgPrivateKey: kp.privateKey,
      wgPublicKey: kp.publicKey,
      wgPresharedKey: psk,
      wgIp: ip,
      updatedAt: new Date(),
    })
    .where(eq(devicesTable.id, deviceId))
    .returning()
  return updated
}

/**
 * Render the WG server config from ALL devices and apply it via `wg syncconf`.
 * A device's peer is included only if its client is active (not frozen and
 * not expired). Call after any change touching devices or client status.
 */
export async function syncWireguardConfig(): Promise<void> {
  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) return

  let serverKeys
  try {
    serverKeys = await loadServerKeys()
  }
  catch {
    return // stage 28 hasn't run yet
  }

  const db = useDb()
  const rows = await db
    .select({
      wgPublicKey: devicesTable.wgPublicKey,
      wgPresharedKey: devicesTable.wgPresharedKey,
      wgIp: devicesTable.wgIp,
      frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))

  const peers = rows
    .filter(r => r.wgPublicKey && r.wgPresharedKey && r.wgIp)
    .map(r => ({
      publicKey: r.wgPublicKey!,
      presharedKey: r.wgPresharedKey!,
      allowedIp: r.wgIp!,
      enabled: isClientActive({ frozenManual: r.frozenManual, expiresAt: r.expiresAt }),
    }))

  await writeWgServerConfig({
    serverPrivateKey: serverKeys.privateKey,
    listenPort: Number(cfg.wgListenPort ?? 51820),
    serverIp: String(cfg.wgServerIp ?? '10.66.66.1'),
    peers,
  })
}
