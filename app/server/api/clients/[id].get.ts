import { requireAuth } from '../../utils/auth'
import { clientCard } from '../../utils/client-view'
import { deviceConfigName } from '../../utils/naming'

/** Детали клиента + его девайсы — для модалки. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { client, devices, operation, status, tgLinked } = await clientCard(event)

  let rxTotal = 0
  let txTotal = 0
  const deviceList = devices.map((d) => {
    rxTotal += d.rxTotal
    txTotal += d.txTotal
    return {
      id: d.id,
      name: d.name,
      configName: deviceConfigName(client.name, d.name, d.id),
      createdAt: d.createdAt,
      rxTotal: d.rxTotal,
      txTotal: d.txTotal,
      hasWg: !!(d.wgPrivateKey && d.wgPublicKey && d.wgIp),
      hasOvpn: !!(d.ovpnCert && d.ovpnKey),
    }
  })

  return {
    operation,
    deleting: client.deleting,
    id: client.id,
    name: client.name,
    filterTraffic: client.filterTraffic,
    expiresAt: client.expiresAt,
    deviceLimit: client.deviceLimit,
    frozenManual: client.frozenManual,
    createdAt: client.createdAt,
    status,
    tgLinked,
    tgUsername: client.tgUsername,
    preferredExit: client.preferredExit,
    rxTotal,
    txTotal,
    devices: deviceList,
  }
})
