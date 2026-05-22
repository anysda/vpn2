import { asc, eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients, devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { clientStatus } from '../../utils/client-status'

/** Детали клиента + его девайсы — для модалки. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const devRows = await db.select().from(devices)
    .where(eq(devices.clientId, id))
    .orderBy(asc(devices.createdAt))

  let rxTotal = 0
  let txTotal = 0
  const deviceList = devRows.map((d) => {
    rxTotal += d.rxTotal
    txTotal += d.txTotal
    return {
      id: d.id,
      name: d.name,
      createdAt: d.createdAt,
      rxTotal: d.rxTotal,
      txTotal: d.txTotal,
      hasWg: !!(d.wgPrivateKey && d.wgPublicKey && d.wgIp),
      hasOvpn: !!(d.ovpnCert && d.ovpnKey),
    }
  })

  return {
    id: client.id,
    name: client.name,
    filterTraffic: client.filterTraffic,
    expiresAt: client.expiresAt,
    deviceLimit: client.deviceLimit,
    frozenManual: client.frozenManual,
    password: client.password,
    createdAt: client.createdAt,
    status: clientStatus(client),
    rxTotal,
    txTotal,
    devices: deviceList,
  }
})
