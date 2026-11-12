import { eq } from 'drizzle-orm'
import { accessStatus } from '../../shared/access-operation'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import type { Client } from '../database/schema'

/** Девайс в карточке клиента: общие поля панели и бота. */
export function deviceView(client: { name: string }, d: typeof devices.$inferSelect) {
  return {
    id: d.id,
    name: d.name,
    configName: deviceConfigName(client.name, d.name, d.id),
    hasWg: !!(d.wgPrivateKey && d.wgPublicKey && d.wgIp),
    hasOvpn: !!(d.ovpnCert && d.ovpnKey),
  }
}

/** Клиент в ответах панели и бота: строка без пароля привязки, чата и служебных полей. */
export function clientFields(client: Client) {
  const { password: _password, tgChatId: _chat, accessVersion: _version, updatedAt: _updated, ...fields } = client
  return fields
}

/** Список клиентов для панели и админского чата бота. */
export async function clientList() {
  const rows = await useDb().select().from(clients).orderBy(...clientsNewestFirst)
  const totals = await deviceTotalsByClient()
  const operations = await currentAccessByClient()

  return rows.map((c) => {
    const t = totals.get(c.id)
    const operation = operations.get(c.id)
    return {
      ...clientFields(c),
      accessStatus: accessStatus(operation ?? null),
      status: clientStatus(c),
      tgLinked: !!c.tgChatId,
      deviceCount: t?.count ?? 0,
      rxTotal: t?.rx ?? 0,
      txTotal: t?.tx ?? 0,
    }
  })
}

export async function deviceById(id: number) {
  const [device] = await useDb().select().from(devices).where(eq(devices.id, id)).limit(1)
  if (!device) throw new Error(`device ${id} not found`)
  return device
}

export async function clientOr404(id: number) {
  const [client] = await useDb().select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw notFound()
  return client
}

/** Клиент с девайсами и последней операцией доступа. */
export async function clientCard(id: number) {
  const client = await clientOr404(id)
  const devRows = await useDb().select().from(devices).where(eq(devices.clientId, id)).orderBy(...devicesOldestFirst)
  const operation = await latestAccess(client.id)
  return {
    client,
    devices: devRows,
    operation,
    accessStatus: accessStatus(operation),
    status: clientStatus(client),
    tgLinked: !!client.tgChatId,
  }
}
