import { requireChatLinked } from '../../../../utils/bot-api'
import { botClientDevice } from '../../../../utils/device-route'
import { deviceOvpnConfig, requireOvpnEndpoint } from '../../../../utils/ovpn-device-config'

// Выдача OpenVPN-конфига девайса по запросу клиента из бота.
export default defineEventHandler(async (event) => {
  const { client, device, ready: endpoint, chatId } = await botClientDevice(event, () => requireOvpnEndpoint())
  const conf = await deviceOvpnConfig(device.id, client.filterTraffic, endpoint)

  // Последний await перед ответом: отвязанный за время сборки чат конфиг не получает.
  await requireChatLinked(client.id, chatId)
  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
