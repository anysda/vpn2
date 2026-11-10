import { botSecretOrThrow, sendBotEvent } from '../../../../../utils/bot-events'
import { adminClientDevice } from '../../../../../utils/device-route'
import { buildIkev2ClientInfo, entryHost, ikev2ServerReady } from '../../../../../utils/ikev2'

/**
 * Послать IKEv2-доступ в Telegram (админский чат). Два сообщения:
 *   1) текст с server/login/password (caption '<username> — IKEv2')
 *   2) ca.crt файлом (caption '<username> — CA-сертификат IKEv2')
 * Платформы не различаем — у нативного IKEv2 на всех ОС одинаковый набор
 * полей и одинаковый CA. Сами .mobileconfig/.sswan не делаем.
 */
export default defineEventHandler(async (event) => {
  const { client, device } = await adminClientDevice(event, async () => {
    if (!(await ikev2ServerReady())) {
      throw createError({ statusCode: 503, statusMessage: 'ikev2_not_deployed', message: 'IKEv2-сервер ещё не развёрнут (стадия 27-ikev2)' })
    }
    botSecretOrThrow()
    if (!entryHost()) throw createError({ statusCode: 500, statusMessage: 'entry_host_not_configured' })
  })

  const info = await buildIkev2ClientInfo(device.id)
  await sendBotEvent({
    type: 'client_send_ikev2',
    clientName: client.name,
    username: info.username,
    server: info.server,
    password: info.password,
    caCertPem: info.caCertPem,
  })
  return { ok: true as const }
})
