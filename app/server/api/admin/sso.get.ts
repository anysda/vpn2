import { requireAuth } from '../../utils/auth'
import { defaultRedirectUrl, discoveryFrom, maskSecret, readSsoStored } from '../../utils/sso'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { stored, fromPanel } = readSsoStored()
  const { clientSecret, ...rest } = stored
  return {
    ...rest,
    // Секрет целиком в браузер не уходит: пустое поле при сохранении = прежний.
    clientSecretMasked: maskSecret(clientSecret),
    hasSecret: !!clientSecret,
    redirectUrl: stored.redirectUrl || defaultRedirectUrl(event),
    discoveryUrl: discoveryFrom(stored.authentikUrl, stored.appSlug),
    source: fromPanel ? 'panel' : 'config',
  }
})
