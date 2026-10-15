import { isPanelReady } from '../utils/panel-ready'

export default defineEventHandler(() => {
  // Стадия 30 ждёт этот ответ перед печатью журнала переноса предпочтений.
  // HTTP начинает слушать раньше завершения async init-плагина Nitro.
  if (!isPanelReady()) {
    throw createError({ statusCode: 503, statusMessage: 'panel_starting', message: 'Панель запускается' })
  }
  return { name: 'anysda-vpn2', version: '1.0.0', license: 'MIT' }
})
