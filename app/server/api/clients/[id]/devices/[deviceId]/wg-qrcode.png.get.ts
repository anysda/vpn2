import { qrPng } from '../../../../../utils/qr'
import { adminDeviceWgConfig } from '../../../../../utils/wg-device-config'

export default defineEventHandler(async (event) => {
  const conf = await adminDeviceWgConfig(event)
  setHeader(event, 'content-type', 'image/png')
  return qrPng(conf)
})
