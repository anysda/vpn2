import { randomBytes } from 'node:crypto'
import { control, controlApply } from './control'
import { and, eq, isNotNull, isNull, or } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { expiryForControl, isClientActive } from './client-status'
import { slugify } from './naming'
import { coalesceRuns } from './coalesce'
import { nextPoolAddress } from './address-pools'
import { clientDns } from './client-dns'

interface Ikev2Profile { ready: boolean, mode: 'letsencrypt' | 'self-signed', host: string, domain: string, ca: string }
function profile(): Promise<Ikev2Profile> { return control('profile.read', { protocol: 'ikev2' }) }

export async function ikev2Mode(): Promise<'letsencrypt' | 'self-signed'> {
  return (await profile()).mode
}

/** Адрес entry для IKEv2: ENTRY_HOST (на него выписан сертификат сервера), иначе публичный адрес WG. */
export function entryHost(): string {
  return String(process.env.ENTRY_HOST || useRuntimeConfig().wgPublicHost || '').trim()
}

/** 20 символов без спецзнаков: пароль вводят руками в системный VPN-клиент. */
function generateIkev2Password(): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  let out = ''
  for (const b of randomBytes(20)) out += alphabet.charAt(b % alphabet.length)
  return out
}

/** `<клиент>-<устройство>`, уникальный среди всех устройств: это id в secrets swanctl. */
function buildIkev2Username(clientName: string, deviceName: string, takenUsernames: Array<string | null>): string {
  const base = `${slugify(clientName)}-${slugify(deviceName)}`
  const taken = new Set(takenUsernames)
  if (!taken.has(base)) return base
  for (let i = 2; i < 1000; i++) {
    if (!taken.has(`${base}-${i}`)) return `${base}-${i}`
  }
  throw new Error(`unable to allocate unique ikev2 username for ${base}`)
}

export async function ikev2ServerReady(): Promise<boolean> { return (await profile()).ready }
export async function ikev2CaReady(): Promise<boolean> { return Boolean((await profile()).ca) }
export async function readIkev2CaPem(): Promise<string> { return (await profile()).ca }

/** Выдаёт устройству логин, пароль и адрес IKEv2, если их нет. Конфликт UNIQUE от параллельной выдачи повторяется. */
export async function ensureDeviceIkev2(deviceId: number) {
  for (let attempt = 1; ; attempt++) {
    try {
      const updated = await claimDeviceIkev2(deviceId)
      if (updated) return updated
    }
    catch (err) {
      if (!isUniqueViolation(err)) throw err
    }
    if (attempt >= IP_CLAIM_ATTEMPTS) throw new Error(`device ${deviceId}: не удалось занять логин и адрес IKEv2`)
  }
}

/** Одна попытка выдать креды. undefined — строку успел дописать параллельный вызов. */
async function claimDeviceIkev2(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)
  if (row.ikev2Username && row.ikev2Password && row.ikev2Ip) return row

  const [client] = await db.select({ name: clientsTable.name }).from(clientsTable).where(eq(clientsTable.id, row.clientId)).limit(1)
  if (!client) throw new Error(`client ${row.clientId} not found`)
  const all = await db.select({ username: devicesTable.ikev2Username, ip: devicesTable.ikev2Ip }).from(devicesTable)

  const [updated] = await db
    .update(devicesTable)
    .set({
      ikev2Username: row.ikev2Username ?? buildIkev2Username(client.name, row.name, all.map(r => r.username)),
      ikev2Password: row.ikev2Password ?? generateIkev2Password(),
      ikev2Ip: row.ikev2Ip ?? nextPoolAddress('ikev2', all.map(r => r.ip)),
      updatedAt: new Date(),
    })
    .where(and(
      eq(devicesTable.id, deviceId),
      row.ikev2Ip ? eq(devicesTable.ikev2Ip, row.ikev2Ip) : isNull(devicesTable.ikev2Ip),
      row.ikev2Username ? eq(devicesTable.ikev2Username, row.ikev2Username) : isNull(devicesTable.ikev2Username),
    ))
    .returning()
  return updated
}


/**
 * Отдаёт сервису управления активных клиентов IKEv2 (замороженные и истёкшие
 * не попадают). У каждого устройства своя conn с постоянным адресом (VPN2-115):
 * системные клиенты называют себя только по EAP-Identity, поэтому общая conn
 * проверяет пароль, а charon затем переходит на conn устройства.
 */
async function syncIkev2Once(): Promise<void> {
  if (!useRuntimeConfig().ikev2Enabled) return
  const db = useDb()

  // Креды выданы не целиком (адрес или логин сняты миграциями 0010 и 0012): довыдаём.
  const orphans = await db
    .select({ id: devicesTable.id })
    .from(devicesTable)
    .where(or(
      and(isNotNull(devicesTable.ikev2Username), isNull(devicesTable.ikev2Ip)),
      and(isNull(devicesTable.ikev2Username), isNotNull(devicesTable.ikev2Ip)),
    ))
  if (orphans.length) {
    // Снимок и выдача в одной write-транзакции: пул читается один раз, параллельный POST значения не займёт.
    await db.transaction(async (tx) => {
      const all = await tx.select({
        id: devicesTable.id, name: devicesTable.name, clientName: clientsTable.name,
        username: devicesTable.ikev2Username, password: devicesTable.ikev2Password,
        ip: devicesTable.ikev2Ip,
      }).from(devicesTable).innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
      const usernames = all.map(r => r.username)
      const ips = all.map(r => r.ip)
      for (const row of all) {
        if (Boolean(row.username) === Boolean(row.ip)) continue
        let ip: string
        let username: string
        try {
          ip = row.ip ?? nextPoolAddress('ikev2', ips)
          username = row.username ?? buildIkev2Username(row.clientName, row.name, usernames)
        }
        catch (err) {
          useLogger().warn({ err: (err as Error).message, id: row.id }, 'ikev2: адрес устройству не выдан')
          continue
        }
        await tx.update(devicesTable).set({
          ikev2Username: username, ikev2Ip: ip,
          ikev2Password: row.password ?? generateIkev2Password(), updatedAt: new Date(),
        }).where(eq(devicesTable.id, row.id))
        if (!row.username) usernames.push(username)
        if (!row.ip) ips.push(ip)
      }
    }, { behavior: 'immediate' })
  }

  const rows = await db
    .select({
      id: devicesTable.id, username: devicesTable.ikev2Username, deleting: devicesTable.deleting,
      password: devicesTable.ikev2Password, ip: devicesTable.ikev2Ip, frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt, filterTraffic: clientsTable.filterTraffic,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))
  const active = rows.filter(r => !r.deleting && r.username && r.password && r.ip && isClientActive(r))

  await controlApply('ikev2.apply', {
    clients: active.map(r => ({ device_id: r.id, username: r.username!, password: r.password!, ip: r.ip!, dns: clientDns(r.filterTraffic), expires_at: expiryForControl(r.expiresAt) })),
  })
}

/** Параллельные вызовы (создание устройств пачкой) идут по очереди, см. coalesceRuns. */
export const syncIkev2 = coalesceRuns(syncIkev2Once)


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
