import { qrSvg } from '../../../../../utils/qr'
import { adminDeviceWgConfig } from '../../../../../utils/wg-device-config'

export default defineEventHandler(async (event) => {
  const conf = await adminDeviceWgConfig(event)
  setHeader(event, 'content-type', 'image/svg+xml; charset=utf-8')
  return qrSvg(conf)
})
