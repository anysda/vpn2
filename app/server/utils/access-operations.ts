import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { and, desc, eq, exists, getTableColumns, lte, ne, notExists, sql } from 'drizzle-orm'
import type { SQL } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { accessProtocols, accessStatus, CONFIRMATION_FRESH_MS, UNCONFIRMED } from '../../shared/access-operation'
import type { AccessKind, AccessOperation } from '../../shared/access-operation'
import { mskDate } from '../../shared/msk'
import { useDb } from '../database/client'
import { accessOperations, botOutbox, clients, devices, events } from '../database/schema'

const RETRY_DELAYS = [5_000, 15_000, 30_000, 60_000, 300_000]
type ClientPatch = Partial<typeof clients.$inferInsert>
interface AccessRequest { clientId: number, deviceId?: number, kind?: AccessKind, patch?: ClientPatch }

/** У вытесненной операции остаётся только целевое состояние. */
const supersededPayload = sql`json_set(${accessOperations.payload}, '$.retired', json('[]'), '$.rotateOvpn', json('[]'))`
const conflict = (message: string) => createError({ statusCode: 409, message })
const keyReused = () => conflict('Ключ идемпотентности уже использован для другого запроса')
const notSuperseded = ne(accessOperations.status, 'superseded')

function requestFingerprint({ clientId, deviceId, kind, patch }: AccessRequest) {
  const fields = patch && Object.fromEntries(Object.entries(patch).filter(([key]) => key !== 'updatedAt').sort(([a], [b]) => a.localeCompare(b)))
  return createHash('sha256').update(JSON.stringify({ clientId, deviceId, kind, patch: fields })).digest('hex')
}

function operationView(row: StoredOperation): AccessOperation {
  const { id, clientId, deviceId, kind, version, status, protocols, nextRetry, createdAt } = row
  return { id, clientId, deviceId, kind, version, status, protocols, nextRetry, createdAt }
}

const operationById = async (id: string) => (await useDb().select().from(accessOperations).where(eq(accessOperations.id, id)))[0]
const latestRow = async (clientId: number) => (await useDb().select().from(accessOperations)
  .where(eq(accessOperations.clientId, clientId)).orderBy(desc(accessOperations.version)).limit(1))[0]

export async function replayAccess(key: string, input: AccessRequest) {
  const [existing] = await useDb().select().from(accessOperations).where(eq(accessOperations.idempotencyKey, key))
  if (!existing) return null
  if (existing.requestHash !== requestFingerprint(input)) throw keyReused()
  return { id: existing.clientId, operation: operationView(existing) }
}

export function accessRequestKey(event: H3Event): string {
  const key = getHeader(event, 'idempotency-key') ?? randomUUID()
  if (!/^[\w:.-]{1,128}$/.test(key)) throw createError({ statusCode: 400, message: 'Неверный ключ идемпотентности' })
  return key
}

/** Версия охватывает клиента целиком: заморозка конфликтует с любым его устройством. */
export async function enqueueAccess(input: AccessRequest & {
  actor: string
  key?: string
  condition?: SQL
  initialize?: boolean
  /** Запрос бота: привязка чата перепроверяется в транзакции записи. */
  chatId?: number
}) {
  const key = input.key ?? randomUUID()
  const requestHash = requestFingerprint(input)
  const enabled = await accessAdapter.enabled()
  return useDb().transaction(async (tx) => {
    if (input.chatId !== undefined) {
      const [linked] = await tx.select({ id: clients.id }).from(clients)
        .where(and(eq(clients.id, input.clientId), eq(clients.tgChatId, input.chatId)))
      if (!linked) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
    }
    const [existing] = await tx.select().from(accessOperations).where(eq(accessOperations.idempotencyKey, key))
    const [before] = await tx.select().from(clients).where(eq(clients.id, input.clientId))
    if (existing) {
      if (existing.requestHash !== requestHash) throw keyReused()
      return { client: before, operation: operationView(existing) }
    }
    if (!before) throw notFound()
    if (before.deleting && input.kind !== 'delete') throw conflict('Клиент удаляется')
    const targets = (await tx.select().from(devices).where(eq(devices.clientId, before.id)))
      .filter(d => input.deviceId === undefined || d.id === input.deviceId)
    if (input.deviceId !== undefined && !targets.length) throw notFound()
    if (input.kind === 'reissue' && targets.some(d => d.deleting)) throw conflict('Устройство удаляется')
    const version = before.accessVersion + (input.initialize ? 0 : 1)
    const patch: ClientPatch = { ...input.patch, accessVersion: version, updatedAt: new Date() }
    if (input.kind === 'delete' && input.deviceId === undefined) Object.assign(patch, { deleting: true, frozenManual: true })
    const [client] = await tx.update(clients).set(patch).where(and(eq(clients.id, before.id), input.condition)).returning()
    if (!client) throw conflict('Клиент изменён параллельным запросом; повторите изменение')
    const kind = input.kind ?? (isClientActive(client) ? 'unfreeze' : 'freeze')
    const [previous] = await tx.select().from(accessOperations)
      .where(and(eq(accessOperations.clientId, before.id), notSuperseded)).orderBy(desc(accessOperations.version)).limit(1)
    const inherited = previous?.status === 'pending' ? carried(previous) : undefined
    const payload: AccessPayload = {
      enabled: !client.deleting && isClientActive(client),
      retired: inherited?.retired ?? [], rotateOvpn: inherited?.rotateOvpn ?? [],
      name: client.name, chatId: input.initialize ? null : client.tgChatId,
    }
    if (input.patch?.expiresAt !== undefined && (before.expiresAt?.getTime() ?? null) !== (client.expiresAt?.getTime() ?? null)) {
      payload.notice = client.expiresAt
        ? `Срок действия вашего ключа: до ${mskDate(client.expiresAt)}.`
        : 'Срок действия ключа снят — доступ бессрочный.'
    }
    if (kind === 'delete' || kind === 'reissue') {
      for (const d of targets) {
        payload.retired = retire(payload.retired, { id: d.id, wgPublicKey: d.wgPublicKey, ovpnCert: d.ovpnCert })
        const unrotated = payload.rotateOvpn.filter(r => r.id !== d.id)
        if (kind === 'delete') {
          await tx.update(devices).set({ deleting: true }).where(eq(devices.id, d.id))
          payload.rotateOvpn = unrotated
          continue
        }
        const wg = wgKeypair()
        await tx.update(devices).set({
          ...(enabled.wg ? { wgPrivateKey: wg.privateKey, wgPublicKey: wg.publicKey, wgPresharedKey: randomBytes(32).toString('base64') } : {}),
          ...(enabled.ikev2 ? { ikev2Password: randomBytes(20).toString('hex') } : {}),
          updatedAt: new Date(),
        }).where(eq(devices.id, d.id))
        if (enabled.openvpn) payload.rotateOvpn = [...unrotated, { id: d.id, cert: d.ovpnCert }]
      }
    }
    await tx.update(accessOperations).set({ status: 'superseded', payload: supersededPayload })
      .where(and(eq(accessOperations.clientId, before.id), notSuperseded))
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

/**
 * Освободилось от 16 МБ - файл ужимается, чтобы бэкапы не копировали пустые
 * страницы. В WAL файл укорачивает только полный checkpoint, поэтому каждый старт.
 */
export async function compactDatabase(): Promise<boolean> {
  const db = useDb()
  const [pages] = await db.all<{ free: number, size: number }>(sql`SELECT freelist_count AS free, page_size AS size FROM pragma_freelist_count(), pragma_page_size()`)
  const vacuumed = !!pages && pages.free * pages.size >= 16 * 1024 * 1024
  if (vacuumed) await db.run(sql`VACUUM`)
  const [checkpoint] = await db.all<{ busy: number }>(sql`PRAGMA wal_checkpoint(TRUNCATE)`)
  if (checkpoint?.busy) throw new Error('database checkpoint busy, file not truncated')
  return vacuumed
}

export async function latestAccess(clientId: number) {
  const row = await latestRow(clientId)
  return row ? operationView(row) : null
}

/** Списки панели и бота читают только текущие версии одним запросом. */
export async function currentAccessByClient() {
  const rows = await useDb().select(getTableColumns(accessOperations)).from(accessOperations)
    .innerJoin(clients, and(eq(clients.id, accessOperations.clientId), eq(clients.accessVersion, accessOperations.version)))
  return new Map(rows.map(row => [row.clientId, operationView(row)]))
}

export async function getAccess(id: string) {
  const row = await operationById(id)
  if (!row) throw notFound()
  return operationView(row)
}

export async function retryAccess(id: string) {
  return useDb().transaction(async (tx) => {
    const [row] = await tx.select().from(accessOperations).where(eq(accessOperations.id, id))
    if (!row) throw notFound()
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

/** Параллельный запрос успел первым: его операция и нужна. */
const unlessConflict = (error: unknown) => {
  if ((error as { statusCode?: number }).statusCode !== 409) throw error
}

/** Клиенты без операций получают первичную сверку, истёкшие - операцию отзыва; ручной флаг не меняется. */
export async function enqueueExpiredAccess() {
  const db = useDb()
  const noOperation = (id: typeof clients.id | number) =>
    notExists(db.select({ id: accessOperations.id }).from(accessOperations).where(eq(accessOperations.clientId, id)))
  for (const client of await db.select().from(clients).where(noOperation(clients.id))) {
    await enqueueAccess({
      clientId: client.id, actor: 'reconcile', initialize: true, key: `initialize:${client.id}:${client.accessVersion}`,
      condition: and(eq(clients.accessVersion, client.accessVersion), noOperation(client.id)),
    }).catch(unlessConflict)
  }
  const expired = await db.select().from(clients)
    .where(and(eq(clients.frozenManual, false), eq(clients.deleting, false), lte(clients.expiresAt, new Date())))
  for (const client of expired) {
    const latest = await latestRow(client.id)
    if (latest && !(JSON.parse(latest.payload) as AccessPayload).enabled) continue
    await enqueueAccess({
      clientId: client.id, actor: 'expiry', key: `expiry:${client.id}:${client.expiresAt!.getTime()}:${client.accessVersion}`,
      condition: eq(clients.accessVersion, client.accessVersion),
    }).catch(unlessConflict)
  }
}

let running: Promise<void> | null = null
/** Единственный исполнитель процесса; cron и ручные повторы используют ту же очередь. */
export function runAccessOperations(adapter: AccessAdapter = accessAdapter): Promise<void> {
  running ??= withAccessLock(() => runOnce(adapter)).finally(() => { running = null })
  return running
}

const isCurrent = (row: StoredOperation) => and(eq(clients.id, row.clientId), eq(clients.accessVersion, row.version))
async function current(row: StoredOperation) {
  return (await useDb().select().from(clients).where(isCurrent(row)))[0]
}
const wanted = (client: typeof clients.$inferSelect) => !client.deleting && isClientActive(client)

async function runOnce(adapter: AccessAdapter) {
  const db = useDb()
  const rows = await db.select().from(accessOperations)
    .where(and(notSuperseded, lte(accessOperations.nextRetry, Date.now()), exists(db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.id, accessOperations.clientId), eq(clients.accessVersion, accessOperations.version))))))
    .orderBy(accessOperations.createdAt).limit(16)
  for (const row of rows) {
    const client = await current(row)
    if (!client) continue
    const payload: AccessPayload = JSON.parse(row.payload)
    if (payload.enabled !== wanted(client)) {
      await enqueueExpiredAccess()
      continue
    }
    const protocols = structuredClone(row.protocols)
    const attempts = row.attempts + 1
    const nextRetry = Date.now() + RETRY_DELAYS[Math.min(attempts - 1, RETRY_DELAYS.length - 1)]!
    for (const p of accessProtocols) {
      if (protocols[p].state !== 'not_required') Object.assign(protocols[p], { lastAttempt: Date.now(), nextRetry })
    }
    // Попытка записана до команды: SIGKILL оставляет всё необходимое для повтора.
    await db.update(accessOperations).set({ attempts, nextRetry, protocols }).where(and(eq(accessOperations.id, row.id), notSuperseded))
    for (const protocol of accessProtocols) {
      if (!await current(row)) break
      const state = protocols[protocol]
      if (state.state === 'not_required') continue
      Object.assign(state, { lastAttempt: Date.now(), nextRetry })
      const unconfirmed = (error: unknown, fallback: string) => Object.assign(state, {
        confirmedVersion: null, checkedAt: null, error: error instanceof ControlUnavailable ? error.message : fallback,
      })
      try {
        // После потерянного ответа изменение вслепую не повторяется.
        let unreadable = false
        let matches = await adapter.read(protocol, row, payload).catch(() => { unreadable = true; return false })
        if (!matches && await current(row)) {
          try { await adapter.apply(protocol, row, payload) }
          catch (error) {
            state.state = unreadable ? 'unknown' : 'failed'
            unconfirmed(error, unreadable ? UNCONFIRMED : 'Не удалось применить изменение')
            continue
          }
          if (!await current(row)) break
          matches = await adapter.read(protocol, row, payload)
        }
        Object.assign(state, {
          state: matches ? 'confirmed' : 'failed', confirmedVersion: matches ? row.version : null,
          checkedAt: Date.now(), error: matches ? null : 'Изменение не подтверждено VPN-службой',
        })
      }
      catch (error) {
        // Ни stdout команд, ни пути, ни ключи не выходят в статус и журнал.
        state.state = 'unknown'
        unconfirmed(error, UNCONFIRMED)
      }
    }
    await recordOutcome(row, payload, protocols, nextRetry)
  }
}

/** Подтверждение, событие, уведомление и удаление цели - одной транзакцией; уникальные ID не дают повторной рассылки. */
async function recordOutcome(row: StoredOperation, payload: AccessPayload, protocols: AccessOperation['protocols'], nextRetry: number) {
  await useDb().transaction(async (tx) => {
    const [fresh] = await tx.select().from(clients).where(isCurrent(row))
    // Срок мог истечь во время команды: следующий проход поставит отзыв.
    if (!fresh || payload.enabled !== wanted(fresh)) return
    const confirmed = accessProtocols.every(p => protocols[p].state === 'not_required' || protocols[p].confirmedVersion === row.version)
    const status = confirmed ? 'confirmed' : 'pending'
    await tx.update(accessOperations).set({
      protocols, status, completionRecorded: confirmed || row.completionRecorded,
      nextRetry: confirmed ? Date.now() + CONFIRMATION_FRESH_MS / 2 : nextRetry,
    }).where(and(eq(accessOperations.id, row.id), notSuperseded))
    if (!confirmed) return
    const title = accessStatus({ ...operationView(row), protocols, status })
    if (!row.completionRecorded) {
      await tx.insert(events).values({
        at: Date.now(), type: 'access_confirmed', level: 'info', object: `client:${row.clientId}`,
        actor: row.actor, title: `${title} · ${payload.name}`, detail: row.actor, key: `access:${row.id}`,
      }).onConflictDoNothing()
      if (payload.chatId && botConfigured()) {
        const now = Date.now()
        await tx.insert(botOutbox).values({
          id: row.id, label: `Уведомление клиенту «${payload.name}»`,
          payload: JSON.stringify({ type: 'client_notify', chatId: payload.chatId, text: [title, payload.notice].filter(Boolean).join('\n') }),
          status: 'queued', due: now, expires: now + 3_600_000, created: now,
        }).onConflictDoNothing()
      }
    }
    await tx.delete(devices).where(and(eq(devices.clientId, row.clientId), eq(devices.deleting, true)))
    if (fresh.deleting) await tx.delete(clients).where(eq(clients.id, fresh.id))
  }, { behavior: 'immediate' })
}
