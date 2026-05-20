import { requireAuth } from '../../../utils/auth'
import { clientDns } from '../../../utils/client-dns'
import { buildWgClientConfig, ensureClientWg, loadServerKeys, syncWireguardConfig } from '../../../utils/wireguard'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) {
    throw createError({ statusCode: 503, statusMessage: 'WireGuard выключен' })
  }
  const endpoint = String(cfg.wgPublicHost || cfg.ssPublicHost || '')
  if (!endpoint) {
    throw createError({ statusCode: 500, statusMessage: 'wg_public_host_not_configured' })
  }

  const server = await loadServerKeys().catch(() => null)
  if (!server) {
    throw createError({ statusCode: 503, statusMessage: 'WireGuard server-ключи ещё не инициализированы (28-wireguard)' })
  }

  const client = await ensureClientWg(id)
  // If we just minted keys, push the server config so the new peer accepts handshakes.
  await syncWireguardConfig().catch(() => {})

  const conf = buildWgClientConfig({
    clientPrivateKey: client.wgPrivateKey!,
    clientPresharedKey: client.wgPresharedKey!,
    clientIp: client.wgIp!,
    serverPublicKey: server.publicKey,
    serverEndpoint: endpoint,
    serverPort: Number(cfg.wgListenPort),
    dns: clientDns(client.filterTraffic).join(', '),
    mtu: Number(cfg.wgMtu),
  })

  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
