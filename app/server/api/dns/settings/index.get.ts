import { requireAuth } from '../../../utils/auth'
import { settingsState, viewOf } from '../../../utils/dns-settings-read'

/** Настройки DNS из AdGuard: серверы выше, защита, кэш, журнал. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return viewOf(await settingsState())
})
