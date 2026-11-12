import { randomBytes } from 'node:crypto'
import { and, eq, isNull } from 'drizzle-orm'
import { claimUnique, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'

interface Ikev2Profile { ready: boolean, mode: 'letsencrypt' | 'self-signed', host: string, domain: string, ca: string }
const profile = (): Promise<Ikev2Profile> => control('profile.read', { protocol: 'ikev2' })

export const ikev2Mode = async () => (await profile()).mode
export const ikev2ServerReady = async () => (await profile()).ready
export const ikev2CaReady = async () => Boolean((await profile()).ca)
export const readIkev2CaPem = async () => (await profile()).ca

/** Адрес entry для IKEv2: ENTRY_HOST (на него выписан сертификат сервера), иначе публичный адрес WG. */
export function entryHost(): string {
  return String(process.env.ENTRY_HOST || useRuntimeConfig().wgPublicHost || '').trim()
}

/** 20 символов без спецзнаков: пароль вводят руками в системный VPN-клиент. */
function generateIkev2Password(): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  return [...randomBytes(20)].map(b => alphabet.charAt(b % alphabet.length)).join('')
}

/** `<клиент>-<устройство>`, уникальный среди всех устройств: это id в secrets swanctl. */
function buildIkev2Username(clientName: string, deviceName: string, takenUsernames: Array<string | null>): string {
  const base = `${slugify(clientName)}-${slugify(deviceName)}`
  const taken = new Set(takenUsernames)
  for (let i = 1; i < 1000; i++) {
    const name = i === 1 ? base : `${base}-${i}`
    if (!taken.has(name)) return name
  }
  throw new Error(`unable to allocate unique ikev2 username for ${base}`)
}

/** Выдаёт устройству логин, пароль и адрес IKEv2, если их нет. */
export function ensureDeviceIkev2(deviceId: number) {
  const db = useDb()
  return claimUnique(`device ${deviceId}: не удалось занять логин и адрес IKEv2`, async () => {
    const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
    if (!row) throw new Error(`device ${deviceId} not found`)
    if (row.ikev2Username && row.ikev2Password && row.ikev2Ip) return row
    const [client] = await db.select({ name: clientsTable.name }).from(clientsTable).where(eq(clientsTable.id, row.clientId)).limit(1)
    if (!client) throw new Error(`client ${row.clientId} not found`)
    const all = await db.select({ username: devicesTable.ikev2Username, ip: devicesTable.ikev2Ip }).from(devicesTable)
    const [updated] = await db.update(devicesTable).set({
      ikev2Username: row.ikev2Username ?? buildIkev2Username(client.name, row.name, all.map(r => r.username)),
      ikev2Password: row.ikev2Password ?? generateIkev2Password(),
      ikev2Ip: row.ikev2Ip ?? nextPoolAddress('ikev2', all.map(r => r.ip)),
      updatedAt: new Date(),
    }).where(and(
      eq(devicesTable.id, deviceId),
      row.ikev2Ip ? eq(devicesTable.ikev2Ip, row.ikev2Ip) : isNull(devicesTable.ikev2Ip),
      row.ikev2Username ? eq(devicesTable.ikev2Username, row.ikev2Username) : isNull(devicesTable.ikev2Username),
    )).returning()
    return updated
  })
}

/**
 * Активные устройства IKEv2 сервису управления. У каждого своя conn с постоянным
 * адресом: системные клиенты называют себя только по EAP-Identity, поэтому общая
 * conn проверяет пароль, а charon затем переходит на conn устройства.
 */
export const syncIkev2 = coalesceRuns(async () => {
  if (!useRuntimeConfig().ikev2Enabled) return
  const rows = await useDb().select({
    id: devicesTable.id, username: devicesTable.ikev2Username, deleting: devicesTable.deleting,
    password: devicesTable.ikev2Password, ip: devicesTable.ikev2Ip, frozenManual: clientsTable.frozenManual,
    expiresAt: clientsTable.expiresAt, filterTraffic: clientsTable.filterTraffic,
  }).from(devicesTable).innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
  const active = rows.filter(r => !r.deleting && r.username && r.password && r.ip && isClientActive(r))
  await controlApply('ikev2.apply', {
    clients: active.map(r => ({
      device_id: r.id, username: r.username!, password: r.password!, ip: r.ip!,
      dns: clientDns(r.filterTraffic), expires_at: expiryForControl(r.expiresAt),
    })),
  })
})

/** Поля для показа клиенту. В режиме Let's Encrypt свой CA не нужен: корень уже в хранилище устройства. */
export async function buildIkev2ClientInfo(deviceId: number) {
  const device = await ensureDeviceIkev2(deviceId)
  const p = await profile()
  const host = p.host || entryHost()
  const domain = p.domain.trim()
  return {
    server: host,
    remoteId: host,
    username: device.ikev2Username!,
    password: device.ikev2Password!,
    caCertPem: p.mode === 'letsencrypt' ? null : (p.ca || null),
    certificatePending: Boolean(domain && (p.mode !== 'letsencrypt' || host !== domain)),
  }
}
