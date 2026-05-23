import { requireAuth } from '../../utils/auth'
import { ikev2CaReady, readIkev2CaPem } from '../../utils/ikev2'

/**
 * Скачать серверный CA как PEM. Нужен Windows/Linux-клиенту чтобы
 * доверить нашему self-signed CA до подключения. На iOS/macOS CA уже
 * зашит внутрь .mobileconfig — этот endpoint там не используется.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  if (!(await ikev2CaReady())) {
    throw createError({ statusCode: 503, statusMessage: 'IKEv2 CA ещё не инициализирован (стадия 27-ikev2)' })
  }
  const pem = await readIkev2CaPem()
  setHeader(event, 'content-type', 'application/x-pem-file')
  setHeader(event, 'content-disposition', 'attachment; filename="anysda-ikev2-ca.crt"')
  return pem
})
