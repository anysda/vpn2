import { requireChatLinked } from '../../../../utils/bot-api'
import { botClientDevice } from '../../../../utils/device-route'
import { qrPng } from '../../../../utils/qr'
import { deviceWgConfig, requireWgEndpoint } from '../../../../utils/wg-device-config'

// QR WireGuard-конфига девайса для бота: тот же генератор, что SVG панели.
export default defineEventHandler(async (event) => {
  const { client, device, chatId, ready: endpoint } = await botClientDevice(event, requireWgEndpoint)
  const png = qrPng(await deviceWgConfig(device.id, client.filterTraffic, endpoint))

  // Последний await перед ответом: отвязанный за время сборки чат QR не получает.
  await requireChatLinked(client.id, chatId)
  setHeader(event, 'content-type', 'image/png')
  return png
})
