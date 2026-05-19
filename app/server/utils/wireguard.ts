import { randomBytes } from 'node:crypto'
import { generateKeyPairSync } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients as clientsTable } from '../database/schema'

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
}

/** Render a client-facing wg-quick .conf string. */
export function buildWgClientConfig(p: WgClientConfigParams): string {
  const dns = p.dns ?? '10.66.66.1'
  const mtu = p.mtu ?? 1420
  return [
    `[Interface]`,
    `PrivateKey = ${p.clientPrivateKey}`,
    `Address = ${p.clientIp}/24`,
    `DNS = ${dns}`,
    `MTU = ${mtu}`,
    ``,
    `[Peer]`,
    `PublicKey = ${p.serverPublicKey}`,
    `PresharedKey = ${p.clientPresharedKey}`,
    `Endpoint = ${p.serverEndpoint}:${p.serverPort}`,
    `AllowedIPs = 0.0.0.0/0, ::/0`,
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
 * sessions. Skipped (with a log warning) if wireguard-tools isn't available
 * — useful for dev where you don't want to require WG userland locally.
 */
export async function writeWgServerConfig(
  p: ServerConfigParams,
  confPath = '/etc/wireguard/wg0.conf',
): Promise<void> {
  const lines: string[] = [
    `# Managed by anysda-vpn2 panel. Do not edit by hand.`,
    `[Interface]`,
    `Address = ${p.serverIp}/24`,
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
      `AllowedIPs = ${peer.allowedIp}/32`,
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
    // hooks), which is exactly what `wg syncconf` accepts.
    await exec('bash', ['-c', `wg-quick strip wg0 | wg syncconf wg0 /dev/stdin`], { timeout: 10_000 })
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
 * stage 28 and read by the panel for peer-config rendering. Throws if the
 * server keys aren't present yet.
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
 * Ensure the given client has WG keys + an IP. If missing, fill them in and
 * persist. Returns the (possibly newly-populated) record.
 */
export async function ensureClientWg(clientId: number) {
  const db = useDb()
  const [row] = await db.select().from(clientsTable).where(eq(clientsTable.id, clientId)).limit(1)
  if (!row) throw new Error(`client ${clientId} not found`)

  if (row.wgPrivateKey && row.wgPublicKey && row.wgPresharedKey && row.wgIp) return row

  const all = await db.select({ ip: clientsTable.wgIp }).from(clientsTable)
  const kp = generateWgKeypair()
  const psk = generateWgPresharedKey()
  const ip = nextAvailableWgIp(all.map(r => r.ip))

  const [updated] = await db
    .update(clientsTable)
    .set({
      wgPrivateKey: row.wgPrivateKey ?? kp.privateKey,
      wgPublicKey: row.wgPublicKey ?? kp.publicKey,
      wgPresharedKey: row.wgPresharedKey ?? psk,
      wgIp: row.wgIp ?? ip,
      updatedAt: new Date(),
    })
    .where(eq(clientsTable.id, clientId))
    .returning()
  return updated
}

/**
 * Pull all enabled clients with WG bound, render the server config and apply
 * it via `wg syncconf`. Call after any change touching the peer set.
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
  const all = await db.select().from(clientsTable)
  const peers = all
    .filter(c => c.wgPublicKey && c.wgPresharedKey && c.wgIp)
    .map(c => ({
      publicKey: c.wgPublicKey!,
      presharedKey: c.wgPresharedKey!,
      allowedIp: c.wgIp!,
      enabled: c.enabled,
    }))

  await writeWgServerConfig({
    serverPrivateKey: serverKeys.privateKey,
    listenPort: Number(cfg.wgListenPort ?? 51820),
    serverIp: String(cfg.wgServerIp ?? '10.66.66.1'),
    peers,
  })
}
