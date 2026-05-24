import { requireAuth } from '../../utils/auth'
import { ikev2CaReady, ikev2Mode, readIkev2CaPem } from '../../utils/ikev2'

/**
 * Скачать серверный CA как PEM. Нужен только в self-signed-режиме.
 * В letsencrypt-режиме — 404: LE-корень уже в trust-store клиентов.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  if ((await ikev2Mode()) === 'letsencrypt') {
    throw createError({
      statusCode: 404,
      statusMessage: 'CA не нужен в letsencrypt-режиме (корневой Let\'s Encrypt уже в trust-store iOS/macOS/Windows/Android)',
    })
  }
  if (!(await ikev2CaReady())) {
    throw createError({ statusCode: 503, statusMessage: 'IKEv2 CA ещё не инициализирован (стадия 27-ikev2)' })
  }
  const pem = await readIkev2CaPem()
  setHeader(event, 'content-type', 'application/x-pem-file')
  setHeader(event, 'content-disposition', 'attachment; filename="anysda-ikev2-ca.crt"')
  return pem
})
