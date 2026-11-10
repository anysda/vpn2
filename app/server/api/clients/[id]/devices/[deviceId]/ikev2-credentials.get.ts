import { adminClientDevice } from '../../../../../utils/device-route'
import { buildIkev2ClientInfo, entryHost, ikev2ServerReady } from '../../../../../utils/ikev2'

/**
 * Возвращает JSON с server/username/password/remoteId + CA cert (PEM).
 * Эти поля UI выводит копируемыми; .mobileconfig — отдельный endpoint.
 */
export default defineEventHandler(async (event) => {
  const { device } = await adminClientDevice(event, async () => {
    if (!(await ikev2ServerReady())) {
      throw createError({ statusCode: 503, statusMessage: 'ikev2_not_deployed', message: 'IKEv2-сервер ещё не развёрнут (стадия 27-ikev2)' })
    }
    // Серверный endpoint для IKEv2 — IP entry (по решению TZ §14.1).
    if (!entryHost()) throw createError({ statusCode: 500, statusMessage: 'entry_host_not_configured' })
  })
  return await buildIkev2ClientInfo(device.id)
})
