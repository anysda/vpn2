import { adminDeviceWgConfig } from '../../../../../utils/wg-device-config'

export default defineEventHandler(async (event) => {
  const conf = await adminDeviceWgConfig(event)
  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
