import { botSecretOrThrow, sendBotEvent } from '../../../../../utils/bot-events'
import { adminClientDevice } from '../../../../../utils/device-route'
import { deviceConfigName } from '../../../../../utils/naming'
import { deviceOvpnConfig, requireOvpnEndpoint } from '../../../../../utils/ovpn-device-config'

export default defineEventHandler(async (event) => {
  // Секрет бота проверяется до готовности CA: без бота 503 bot_not_configured при любом CA.
  const { client, device, ready: endpoint } = await adminClientDevice(event, () => requireOvpnEndpoint(botSecretOrThrow))
  const conf = await deviceOvpnConfig(device.id, client.filterTraffic, endpoint)
  const name = deviceConfigName(client.name, device.name, device.id)
  await sendBotEvent({ type: 'client_send_openvpn', name, conf })
  return { ok: true }
})
