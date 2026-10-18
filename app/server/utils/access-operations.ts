import { createHash, generateKeyPairSync, randomBytes, randomUUID } from 'node:crypto'
import { and, desc, eq, exists, getTableColumns, lte, ne, notExists } from 'drizzle-orm'
import type { SQL } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { accessProtocols, accessStatus, CONFIRMATION_FRESH_MS } from '../../shared/access-operation'
import type { AccessKind, AccessOperation } from '../../shared/access-operation'
import { useDb } from '../database/client'
import { accessOperations, botOutbox, clients, devices, events } from '../database/schema'
import type { Client } from '../database/schema'
import { isClientActive } from './client-status'
import { accessAdapter } from './access-adapter'
import type { AccessAdapter } from './access-adapter'
import { botConfigured } from './telegram-config'
import { withAccessLock } from './access-lock'

interface RetiredAccess { id: number, wgPublicKey: string | null, ovpnCert: string | null }
export interface AccessPayload {
  enabled: boolean
  retired: RetiredAccess[]
  rotateOvpn: Array<{ id: number, cert: string | null }>
  name: string
  chatId: number | null
  notice?: string
}
type StoredOperation = typeof accessOperations.$inferSelect
const RETRY_DELAYS = [5_000, 15_000, 30_000, 60_000, 300_000]

interface AccessRequest {
  clientId: number
  deviceId?: number
  kind?: AccessKind
  patch?: Partial<typeof clients.$inferInsert>
}

function requestFingerprint(input: AccessRequest) {
  const patch = input.patch && Object.fromEntries(Object.entries(input.patch)
    .filter(([key]) => key !== 'updatedAt').sort(([a], [b]) => a.localeCompare(b)))
  return createHash('sha256').update(JSON.stringify({ clientId: input.clientId, deviceId: input.deviceId, kind: input.kind, patch })).digest('hex')
}

export async function replayAccess(key: string, input: AccessRequest) {
  const [existing] = await useDb().select().from(accessOperations).where(eq(accessOperations.idempotencyKey, key))
  if (!existing) return null
  if (existing.requestHash !== requestFingerprint(input)) throw createError({ statusCode: 409, message: 'Ключ идемпотентности уже использован для другого запроса' })
  return { id: existing.clientId, operation: operationView(existing) }
}

function operationView(row: StoredOperation): AccessOperation {
  const { id, clientId, deviceId, kind, version, status, protocols, nextRetry, createdAt } = row
  return { id, clientId, deviceId, kind, version, status, protocols, nextRetry, createdAt }
}

export function accessRequestKey(event: H3Event): string {
  const key = getHeader(event, 'idempotency-key') ?? randomUUID()
  if (!/^[\w:.-]{1,128}$/.test(key)) throw createError({ statusCode: 400, message: 'Неверный ключ идемпотентности' })
  return key
}

/** Версия охватывает клиента целиком: заморозка конфликтует с любым его устройством. */
export async function enqueueAccess(input: {
  clientId: number
  deviceId?: number
  kind?: AccessKind
  actor: string
  key?: string
  patch?: Partial<typeof clients.$inferInsert>
  condition?: SQL
  initialize?: boolean
}) {
  const db = useDb()
  const key = input.key ?? randomUUID()
  const requestHash = requestFingerprint(input)
  const enabled = await accessAdapter.enabled()
  return db.transaction(async (tx) => {
    const [existing] = await tx.select().from(accessOperations).where(eq(accessOperations.idempotencyKey, key))
    if (existing) {
      if (existing.requestHash !== requestHash) throw createError({ statusCode: 409, message: 'Ключ идемпотентности уже использован для другого запроса' })
      const [client] = await tx.select().from(clients).where(eq(clients.id, input.clientId))
      return { client, operation: operationView(existing) }
    }
    const [before] = await tx.select().from(clients).where(eq(clients.id, input.clientId))
    if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    if (before.deleting && input.kind !== 'delete') throw createError({ statusCode: 409, message: 'Клиент удаляется' })
    const allDevices = await tx.select().from(devices).where(eq(devices.clientId, before.id))
    const targets = allDevices.filter(d => input.deviceId === undefined || d.id === input.deviceId)
    if (input.deviceId !== undefined && !targets.length) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    if (input.kind === 'reissue' && targets.some(d => d.deleting)) throw createError({ statusCode: 409, message: 'Устройство удаляется' })
    const version = before.accessVersion + (input.initialize ? 0 : 1)
    const patch = { ...input.patch, accessVersion: version, updatedAt: new Date() }
    if (input.kind === 'delete' && input.deviceId === undefined) Object.assign(patch, { deleting: true, frozenManual: true })
    const [client] = await tx.update(clients).set(patch)
      .where(and(eq(clients.id, before.id), input.condition)).returning()
    if (!client) throw createError({ statusCode: 409, message: 'Клиент изменён параллельным запросом; повторите изменение' })
    const kind = input.kind ?? (isClientActive(client) ? 'unfreeze' : 'freeze')
    const [previous] = await tx.select().from(accessOperations)
      .where(and(eq(accessOperations.clientId, before.id), ne(accessOperations.status, 'superseded')))
      .orderBy(desc(accessOperations.version)).limit(1)
    const inherited: AccessPayload | undefined = previous?.status === 'pending' ? JSON.parse(previous.payload) : undefined
    const payload: AccessPayload = {
      enabled: !client.deleting && isClientActive(client),
      retired: inherited?.retired ?? [], rotateOvpn: inherited?.rotateOvpn ?? [],
      name: client.name, chatId: input.initialize ? null : client.tgChatId,
    }
    if (input.patch?.expiresAt !== undefined && (before.expiresAt?.getTime() ?? null) !== (client.expiresAt?.getTime() ?? null)) {
      payload.notice = client.expiresAt
        ? `Срок действия вашего ключа: до ${client.expiresAt.toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' })}.`
        : 'Срок действия ключа снят — доступ бессрочный.'
    }
    if (kind === 'delete' || kind === 'reissue') {
      for (const d of targets) {
        payload.retired.push({ id: d.id, wgPublicKey: d.wgPublicKey, ovpnCert: d.ovpnCert })
        if (kind === 'delete') {
          await tx.update(devices).set({ deleting: true }).where(eq(devices.id, d.id))
          payload.rotateOvpn = payload.rotateOvpn.filter(r => r.id !== d.id)
        }
        else {
          const kp = generateKeyPairSync('x25519')
          await tx.update(devices).set({
            ...(enabled.wg ? {
              wgPrivateKey: (kp.privateKey.export({ format: 'der', type: 'pkcs8' }) as Buffer).subarray(-32).toString('base64'),
              wgPublicKey: (kp.publicKey.export({ format: 'der', type: 'spki' }) as Buffer).subarray(-32).toString('base64'),
              wgPresharedKey: randomBytes(32).toString('base64'),
            } : {}),
            ...(enabled.ikev2 ? { ikev2Password: randomBytes(20).toString('hex') } : {}),
            updatedAt: new Date(),
          }).where(eq(devices.id, d.id))
          if (enabled.openvpn) {
            payload.rotateOvpn = payload.rotateOvpn.filter(r => r.id !== d.id)
            payload.rotateOvpn.push({ id: d.id, cert: d.ovpnCert })
          }
        }
      }
    }
    await tx.update(accessOperations).set({ status: 'superseded' })
      .where(and(eq(accessOperations.clientId, before.id), ne(accessOperations.status, 'superseded')))
    const now = Date.now()
    const protocols = Object.fromEntries(accessProtocols.map(p => [p, {
      state: enabled[p] ? 'pending' : 'not_required', confirmedVersion: null, checkedAt: null,
      lastAttempt: null, nextRetry: enabled[p] ? now : null, error: null,
    }])) as AccessOperation['protocols']
    const [row] = await tx.insert(accessOperations).values({
      id: randomUUID(), clientId: before.id, deviceId: input.deviceId ?? null,
      kind, version, idempotencyKey: key, requestHash, status: 'pending', protocols,
      completionRecorded: Boolean(input.initialize),
      payload: JSON.stringify(payload), actor: input.actor, nextRetry: now, createdAt: now,
    }).returning()
    return { client, operation: operationView(row!) }
  }, { behavior: 'immediate' })
}

export async function latestAccess(clientId: number) {
  const [row] = await useDb().select().from(accessOperations).where(eq(accessOperations.clientId, clientId))
    .orderBy(desc(accessOperations.version)).limit(1)
  return row ? operationView(row) : null
}

/** Списки панели и бота читают только текущие версии одним запросом. */
export async function currentAccessByClient() {
  const rows = await useDb().select(getTableColumns(accessOperations)).from(accessOperations)
    .innerJoin(clients, and(eq(clients.id, accessOperations.clientId), eq(clients.accessVersion, accessOperations.version)))
  return new Map(rows.map(row => [row.clientId, operationView(row)]))
}

export async function getAccess(id: string) {
  const [row] = await useDb().select().from(accessOperations).where(eq(accessOperations.id, id))
  if (!row) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  return operationView(row)
}

export async function retryAccess(id: string) {
  const db = useDb()
  return db.transaction(async (tx) => {
    const [row] = await tx.select().from(accessOperations).where(eq(accessOperations.id, id))
    if (!row) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    const operation = operationView(row)
    if (operation.status === 'superseded' || (operation.status === 'confirmed' && operation.kind === 'delete' && operation.deviceId === null)) return operation
    const lastAttempt = Math.max(0, ...accessProtocols.map(p => operation.protocols[p].lastAttempt ?? 0))
    const requested = Math.max(Date.now(), lastAttempt + RETRY_DELAYS[0]!)
    if (requested >= row.nextRetry) return operation
    const protocols = structuredClone(row.protocols)
    for (const state of Object.values(protocols)) if (state.state !== 'not_required') state.nextRetry = requested
    const [updated] = await tx.update(accessOperations).set({ status: 'pending', nextRetry: requested, protocols })
      .where(eq(accessOperations.id, id)).returning()
    return operationView(updated!)
  }, { behavior: 'immediate' })
}

/** Истечение срока также получает долговечную операцию, без изменения ручного флага. */
export async function enqueueExpiredAccess() {
  const db = useDb()
  // Первичная сверка прежней базы сохраняет самовосстановление cron.
  // Она не меняет желаемое состояние/версию и не отменяет выдачу в процессе.
  const untracked = await db.select().from(clients).where(notExists(
    db.select({ id: accessOperations.id }).from(accessOperations).where(eq(accessOperations.clientId, clients.id)),
  ))
  for (const client of untracked) {
    try {
      await enqueueAccess({ clientId: client.id, actor: 'reconcile', initialize: true,
        key: `initialize:${client.id}:${client.accessVersion}`,
        condition: and(eq(clients.accessVersion, client.accessVersion), notExists(
          db.select({ id: accessOperations.id }).from(accessOperations).where(eq(accessOperations.clientId, client.id)),
        )) })
    }
    catch (error) {
      if ((error as { statusCode?: number }).statusCode !== 409) throw error
    }
  }
  const expired = await db.select().from(clients).where(and(
    eq(clients.frozenManual, false), eq(clients.deleting, false), lte(clients.expiresAt, new Date()),
  ))
  for (const client of expired) {
    const [latest] = await db.select().from(accessOperations).where(eq(accessOperations.clientId, client.id))
      .orderBy(desc(accessOperations.version)).limit(1)
    if (latest && !(JSON.parse(latest.payload) as AccessPayload).enabled) continue
    try {
      await enqueueAccess({ clientId: client.id, actor: 'expiry', key: `expiry:${client.id}:${client.expiresAt!.getTime()}:${client.accessVersion}`,
        condition: eq(clients.accessVersion, client.accessVersion) })
    }
    catch (error) {
      if ((error as { statusCode?: number }).statusCode !== 409) throw error
    }
  }
}

let running: Promise<void> | null = null
/** Единственный исполнитель процесса; cron и ручные повторы используют ту же очередь. */
export function runAccessOperations(adapter: AccessAdapter = accessAdapter): Promise<void> {
  if (running) return running
  running = withAccessLock(() => runOnce(adapter)).finally(() => { running = null })
  return running
}

async function current(row: StoredOperation): Promise<Client | undefined> {
  const [client] = await useDb().select().from(clients)
    .where(and(eq(clients.id, row.clientId), eq(clients.accessVersion, row.version)))
  return client
}

async function runOnce(adapter: AccessAdapter) {
  const db = useDb()
  const rows = await db.select().from(accessOperations)
    .where(and(ne(accessOperations.status, 'superseded'), lte(accessOperations.nextRetry, Date.now()),
      exists(db.select({ id: clients.id }).from(clients).where(and(
        eq(clients.id, accessOperations.clientId), eq(clients.accessVersion, accessOperations.version),
      )))))
    .orderBy(accessOperations.createdAt).limit(16)
  for (const row of rows) {
    const client = await current(row)
    // Удалённая цель уже подтверждена; её историческое подтверждение не обновляется.
    if (!client) continue
    const payload: AccessPayload = JSON.parse(row.payload)
    if (payload.enabled !== (!client.deleting && isClientActive(client))) {
      await enqueueExpiredAccess()
      continue
    }
    const protocols = structuredClone(row.protocols)
    const attempts = row.attempts + 1
    const nextRetry = Date.now() + RETRY_DELAYS[Math.min(attempts - 1, RETRY_DELAYS.length - 1)]!
    for (const p of accessProtocols) {
      if (protocols[p].state !== 'not_required') Object.assign(protocols[p], { lastAttempt: Date.now(), nextRetry })
    }
    // Записываем попытку ДО команды. SIGKILL оставляет всё необходимое для повтора.
    await db.update(accessOperations).set({ attempts, nextRetry, protocols }).where(and(eq(accessOperations.id, row.id), ne(accessOperations.status, 'superseded')))
    for (const protocol of accessProtocols) {
      if (!await current(row)) break
      const state = protocols[protocol]
      if (state.state === 'not_required') continue
      state.lastAttempt = Date.now()
      state.nextRetry = nextRetry
      try {
        // После потерянного ответа никогда не повторяем изменение вслепую.
        let unreadable = false
        let matches = await adapter.read(protocol, row, payload).catch(() => { unreadable = true; return false })
        if (!matches && await current(row)) {
          try { await adapter.apply(protocol, row, payload) }
          catch {
            state.state = unreadable ? 'unknown' : 'failed'
            state.confirmedVersion = null
            state.checkedAt = null
            state.error = unreadable ? 'Состояние не подтверждено' : 'Не удалось применить изменение'
            continue
          }
          if (!await current(row)) break
          matches = await adapter.read(protocol, row, payload)
        }
        state.state = matches ? 'confirmed' : 'failed'
        state.confirmedVersion = matches ? row.version : null
        state.checkedAt = Date.now()
        state.error = matches ? null : 'Изменение не подтверждено VPN-службой'
      }
      catch {
        // Ни stdout команд, ни пути, ни ключи не выходят в статус/журнал.
        state.state = 'unknown'
        state.confirmedVersion = null
        state.checkedAt = null
        state.error = 'Состояние не подтверждено'
      }
    }
    await db.transaction(async (tx) => {
      const [fresh] = await tx.select().from(clients).where(and(eq(clients.id, row.clientId), eq(clients.accessVersion, row.version)))
      if (!fresh) return
      const confirmed = accessProtocols.every(p => protocols[p].state === 'not_required' || protocols[p].confirmedVersion === row.version)
      const status = confirmed ? 'confirmed' : 'pending'
      await tx.update(accessOperations).set({ protocols, status, completionRecorded: confirmed || row.completionRecorded, nextRetry: confirmed ? Date.now() + CONFIRMATION_FRESH_MS / 2 : nextRetry })
        .where(and(eq(accessOperations.id, row.id), ne(accessOperations.status, 'superseded')))
      if (!confirmed) return
      const title = accessStatus({ ...operationView(row), protocols, status })
      // Событие + доставка + удаление цели фиксируются вместе с подтверждением.
      // Уникальные ID не допускают повторной рассылки после рестарта.
      const eventKey = `access:${row.id}`
      if (!row.completionRecorded) await tx.insert(events).values({
        at: Date.now(), type: 'access_confirmed', level: 'info', object: `client:${row.clientId}`,
        actor: row.actor, title: `${title} · ${payload.name}`, detail: row.actor, key: eventKey,
      }).onConflictDoNothing()
      if (!row.completionRecorded && payload.chatId && botConfigured()) {
        const now = Date.now()
        await tx.insert(botOutbox).values({
          id: row.id, label: `Уведомление клиенту «${payload.name}»`,
          payload: JSON.stringify({ type: 'client_notify', chatId: payload.chatId, text: [title, payload.notice].filter(Boolean).join('\n') }),
          status: 'queued', due: now, expires: now + 3_600_000, created: now,
        }).onConflictDoNothing()
      }
      await tx.delete(devices).where(and(eq(devices.clientId, row.clientId), eq(devices.deleting, true)))
      if (fresh.deleting) await tx.delete(clients).where(eq(clients.id, fresh.id))
    }, { behavior: 'immediate' })
  }
}
