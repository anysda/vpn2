import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { notifyBot } from '../../utils/bot-events'
import { generateSsSecret, syncShadowsocksConfig } from '../../utils/shadowsocks'
import { buildSsUrl } from '../../utils/ss-url'
import {
  buildWgClientConfig,
  generateWgKeypair,
  generateWgPresharedKey,
  loadServerKeys,
  nextAvailableWgIp,
  syncWireguardConfig,
} from '../../utils/wireguard'
import { buildOvpnConfig, caReady, ensureClientOvpn } from '../../utils/openvpn'

const Body = z.object({
  name: z.string().min(1).max(64),
  expiresAt: z.iso.datetime().nullable().optional(),
  sendSsToTg: z.boolean().optional(),
  sendWgToTg: z.boolean().optional(),
  sendOvpnToTg: z.boolean().optional(),
  // legacy alias for older clients of this API
  sendToTg: z.boolean().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const cfg = useRuntimeConfig()
  const db = useDb()

  const name = body.name.trim()
  const dup = await db.select({ id: clients.id }).from(clients).where(eq(clients.name, name)).limit(1)
  if (dup.length > 0) {
    throw createError({ statusCode: 409, statusMessage: `Клиент с именем «${name}» уже существует` })
  }

  // Pre-mint WG identity at create time so both protocols are immediately
  // available — same UX as SS, no lazy keygen-on-first-fetch.
  const allWg = await db.select({ ip: clients.wgIp }).from(clients)
  const wgKp = generateWgKeypair()
  const wgPsk = generateWgPresharedKey()
  const wgIp = nextAvailableWgIp(allWg.map(r => r.ip))

  const [row] = await db
    .insert(clients)
    .values({
      name,
      ssSecret: generateSsSecret(),
      cipher: cfg.ssCipher,
      enabled: true,
      expiresAt: body.expiresAt ? new Date(body.expiresAt) : null,
      wgPrivateKey: wgKp.privateKey,
      wgPublicKey: wgKp.publicKey,
      wgPresharedKey: wgPsk,
      wgIp,
    })
    .returning()

  await syncShadowsocksConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync ss config after create')
  })
  await syncWireguardConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync wg config after create')
  })

  void notifyBot('client_created', { name: row.name })

  const sendSs = Boolean(body.sendSsToTg ?? body.sendToTg)
  const sendWg = Boolean(body.sendWgToTg)

  if (sendSs && cfg.ssPublicHost) {
    const ssUrl = buildSsUrl({
      cipher: row.cipher,
      secret: row.ssSecret,
      host: String(cfg.ssPublicHost),
      port: Number(cfg.ssPort),
      name: row.name,
    })
    void notifyBot('client_send_config', { name: row.name, ssUrl })
  }

  if (sendWg && cfg.wgEnabled) {
    const endpoint = String(cfg.wgPublicHost || cfg.ssPublicHost || '')
    const server = await loadServerKeys().catch(() => null)
    if (endpoint && server) {
      const conf = buildWgClientConfig({
        clientPrivateKey: row.wgPrivateKey!,
        clientPresharedKey: row.wgPresharedKey!,
        clientIp: row.wgIp!,
        serverPublicKey: server.publicKey,
        serverEndpoint: endpoint,
        serverPort: Number(cfg.wgListenPort),
        dns: String(cfg.wgDns),
        mtu: Number(cfg.wgMtu),
      })
      void notifyBot('client_send_wireguard', { name: row.name, conf })
    }
  }

  if (body.sendOvpnToTg && cfg.ovpnEnabled) {
    const endpoint = String(cfg.ovpnPublicHost || cfg.ssPublicHost || '')
    // OpenVPN cert issuance shells out to the CA — best-effort, don't let a
    // not-yet-initialised CA fail the whole client creation.
    try {
      if (endpoint && await caReady()) {
        const client = await ensureClientOvpn(row.id)
        const conf = await buildOvpnConfig({
          clientCert: client.ovpnCert!,
          clientKey: client.ovpnKey!,
          serverHost: endpoint,
          serverPort: Number(cfg.ovpnPort),
          proto: String(cfg.ovpnProto),
        })
        void notifyBot('client_send_openvpn', { name: row.name, conf })
      }
    }
    catch (err) {
      useLogger().error({ err }, 'failed to send openvpn config after create')
    }
  }

  return row
})
