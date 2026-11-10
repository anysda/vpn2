import { adminClientDevice } from '../../../../../utils/device-route'
import { deviceOvpnConfig, requireOvpnEndpoint } from '../../../../../utils/ovpn-device-config'

export default defineEventHandler(async (event) => {
  const { client, device, ready: endpoint } = await adminClientDevice(event, () => requireOvpnEndpoint())
  const conf = await deviceOvpnConfig(device.id, client.filterTraffic, endpoint)
  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
