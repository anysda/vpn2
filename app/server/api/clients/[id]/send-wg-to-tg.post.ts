import { requireAuth } from '../../../utils/auth'
import { buildWgClientConfig, ensureClientWg, loadServerKeys, syncWireguardConfig } from '../../../utils/wireguard'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) throw createError({ statusCode: 503, statusMessage: 'WireGuard выключен' })
  const endpoint = String(cfg.wgPublicHost || cfg.ssPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'wg_public_host_not_configured' })

  const secret = String(cfg.tgbotSecret ?? '')
  if (!secret) throw createError({ statusCode: 503, statusMessage: 'Telegram-бот не настроен' })

  const server = await loadServerKeys().catch(() => null)
  if (!server) {
    throw createError({ statusCode: 503, statusMessage: 'WireGuard server-ключи ещё не инициализированы (28-wireguard)' })
  }

  const client = await ensureClientWg(id)
  await syncWireguardConfig().catch(() => {})

  const conf = buildWgClientConfig({
    clientPrivateKey: client.wgPrivateKey!,
    clientPresharedKey: client.wgPresharedKey!,
    clientIp: client.wgIp!,
    serverPublicKey: server.publicKey,
    serverEndpoint: endpoint,
    serverPort: Number(cfg.wgListenPort),
    dns: String(cfg.wgDns),
    mtu: Number(cfg.wgMtu),
  })

  const port = Number(cfg.tgbotEventPort ?? 8877)
  try {
    await $fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'X-Tgbot-Secret': secret },
      body: { type: 'client_send_wireguard', name: client.name, conf },
      timeout: 8000,
    })
  }
  catch (e) {
    throw createError({
      statusCode: 502,
      statusMessage: 'Бот не отвечает — проверь /api/admin/telegram',
      cause: e,
    })
  }

  return { ok: true }
})
