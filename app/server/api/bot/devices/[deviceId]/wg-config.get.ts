import { requireChatLinked } from '../../../../utils/bot-api'
import { botClientDevice } from '../../../../utils/device-route'
import { deviceWgConfig, requireWgEndpoint } from '../../../../utils/wg-device-config'

// Выдача WireGuard-конфига девайса по запросу клиента из бота.
export default defineEventHandler(async (event) => {
  const { client, device, chatId, ready: endpoint } = await botClientDevice(event, requireWgEndpoint)
  const conf = await deviceWgConfig(device.id, client.filterTraffic, endpoint)

  // Последний await перед ответом: отвязанный за время сборки чат конфиг не получает.
  await requireChatLinked(client.id, chatId)
  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
