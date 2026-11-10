import { botSecretOrThrow, sendBotEvent } from '../../../../../utils/bot-events'
import { adminClientDevice } from '../../../../../utils/device-route'
import { deviceConfigName } from '../../../../../utils/naming'
import { deviceWgConfig, requireWgEndpoint } from '../../../../../utils/wg-device-config'

export default defineEventHandler(async (event) => {
  // Секрет бота - до поиска в базе: без бота 503 и для чужого id.
  const { client, device, ready: endpoint } = await adminClientDevice(event, () => {
    const host = requireWgEndpoint()
    botSecretOrThrow()
    return host
  })
  const conf = await deviceWgConfig(device.id, client.filterTraffic, endpoint)

  const name = deviceConfigName(client.name, device.name, device.id)
  await sendBotEvent({ type: 'client_send_wireguard', name, conf })

  return { ok: true }
})
