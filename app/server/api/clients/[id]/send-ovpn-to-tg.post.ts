import { requireAuth } from '../../../utils/auth'
import { buildOvpnConfig, caReady, ensureClientOvpn } from '../../../utils/openvpn'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) throw createError({ statusCode: 503, statusMessage: 'OpenVPN выключен' })
  const endpoint = String(cfg.ovpnPublicHost || cfg.ssPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'ovpn_public_host_not_configured' })

  const secret = String(cfg.tgbotSecret ?? '')
  if (!secret) throw createError({ statusCode: 503, statusMessage: 'Telegram-бот не настроен' })

  if (!(await caReady())) {
    throw createError({ statusCode: 503, statusMessage: 'OpenVPN CA ещё не инициализирован (29-openvpn)' })
  }

  const client = await ensureClientOvpn(id)
  const conf = await buildOvpnConfig({
    clientCert: client.ovpnCert!,
    clientKey: client.ovpnKey!,
    serverHost: endpoint,
    serverPort: Number(cfg.ovpnPort),
    proto: String(cfg.ovpnProto),
  })

  const port = Number(cfg.tgbotEventPort ?? 8877)
  try {
    await $fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'X-Tgbot-Secret': secret },
      body: { type: 'client_send_openvpn', name: client.name, conf },
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
