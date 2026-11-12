import { and, count, eq, ne, notExists, sql } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../database/client'
import type { Client } from '../database/schema'
import { clients, devices } from '../database/schema'
import { notifyBot, notifyClient, notifyWarning } from './bot-events'
import { minExpiryMs } from './expiry'
import { generateClientPassword } from './password'
import { enqueueAccess, replayAccess } from './access-operations'
import type { AccessOperation } from '../../shared/access-operation'
import { syncClientPrefsFile } from './client-prefs'
import { assertKnownOutbound } from './route-outbound'
import { joinDetail, recordEvent } from './events'
import { plural } from '../../shared/plural'

const Name = z.string().trim().min(1).max(64)
const Limits = {
  expiresAt: z.iso.datetime().nullable().optional(),
  // null - безлимит.
  deviceLimit: z.number().int().min(1).max(999).nullable().optional(),
}
export const ClientCreate = z.object({ name: Name, filterTraffic: z.boolean().optional(), ...Limits })
export const ClientPatch = z.object({
  name: Name.optional(),
  ...Limits,
  frozenManual: z.boolean().optional(),
  // тег узла экзита (hy2-<тег>-direct должен существовать) или null - авто
  preferredExit: z.string().regex(/^[a-z0-9-]{1,32}$/).nullable().optional(),
})
export type ClientCreateInput = z.infer<typeof ClientCreate>
export type ClientPatch = z.infer<typeof ClientPatch>
type ClientUpdate = Partial<typeof clients.$inferInsert>

export const deviceLimitBelowCountText = (have: number) => `У клиента уже ${have} ${plural(have, 'device')} — лимит не может быть меньше`
const msk = (date: Date) => date.toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' })
const time = (date: Date | null | undefined) => date?.getTime() ?? null
const deviceCount = async (clientId: number) =>
  Number((await useDb().select({ n: count() }).from(devices).where(eq(devices.clientId, clientId)))[0]?.n ?? 0)

/** Срок действия не раньше завтрашней даты. */
function assertExpiryAllowed(expiresAt: string | null | undefined): void {
  if (expiresAt && new Date(expiresAt).getTime() < minExpiryMs()) {
    throw createError({ statusCode: 422, statusMessage: 'invalid_expiry', message: 'Срок действия не может быть раньше завтрашней даты' })
  }
}

async function assertNameFree(name: string, exceptId = 0): Promise<void> {
  const dup = await useDb().select({ id: clients.id }).from(clients).where(and(eq(clients.name, name), ne(clients.id, exceptId))).limit(1)
  if (dup.length) throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${name}» уже существует` })
}

export async function createClient(input: ClientCreateInput, actor: string): Promise<Client & { warning?: string }> {
  assertExpiryAllowed(input.expiresAt)
  const name = input.name.trim()
  await assertNameFree(name)
  const values: typeof clients.$inferInsert = { name, filterTraffic: input.filterTraffic ?? true, password: generateClientPassword() }
  if (input.expiresAt !== undefined) values.expiresAt = input.expiresAt ? new Date(input.expiresAt) : null
  if (input.deviceLimit !== undefined) values.deviceLimit = input.deviceLimit
  const [row] = await useDb().insert(clients).values(values).returning()
  if (!row) throw createError({ statusCode: 500, statusMessage: 'insert_failed' })
  // Клиент уже создан: сбой очереди бота - предупреждение в ответе, не ошибка.
  const warning = await notifyWarning(notifyBot('client_created', { name: row.name }), 'Уведомление о клиенте не отправлено')
  await recordEvent({ type: 'client_created', level: 'info', object: `client:${row.id}`, actor, title: `Клиент создан · ${row.name}`, detail: actor })
  return { ...row, ...(warning ? { warning } : {}) }
}

/** Поля правки в виде записи в базу; без updatedAt - по ним же сверяется повтор по ключу. */
function normalized(patch: ClientPatch): ClientUpdate {
  const upd: ClientUpdate = {}
  if (patch.name !== undefined) upd.name = patch.name.trim()
  if (patch.expiresAt !== undefined) upd.expiresAt = patch.expiresAt ? new Date(patch.expiresAt) : null
  if (patch.deviceLimit !== undefined) upd.deviceLimit = patch.deviceLimit
  if (patch.frozenManual !== undefined) upd.frozenManual = patch.frozenManual
  if (patch.preferredExit !== undefined) upd.preferredExit = patch.preferredExit || null
  return upd
}

/** Текст клиенту о смене лимита устройств; пусто - лимит не изменился. */
function limitText(before: number | null, after: number | null): string {
  const rank = (v: number | null) => v ?? Infinity
  if (rank(after) > rank(before)) return after === null ? '📈 Лимит устройств снят — теперь без ограничений.' : `📈 Ваш лимит устройств повышен до ${after}.`
  return rank(after) < rank(before) ? `📉 Ваш лимит устройств понижен до ${after}.` : ''
}

type Updated = Partial<Client> & { id: number, operation?: AccessOperation, warning?: string }

/** Проверки, запись, операция доступа (заморозка и срок), журнал и уведомления клиенту. */
export async function updateClient(id: number, patch: ClientPatch, actor: string, key?: string): Promise<Updated> {
  const db = useDb()
  const changesAccess = patch.frozenManual !== undefined || patch.expiresAt !== undefined
  const upd = normalized(patch)
  if (key && changesAccess) {
    const replay = await replayAccess(key, { clientId: id, patch: upd })
    if (replay) return replay
  }
  assertExpiryAllowed(patch.expiresAt)
  const [before] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  if (typeof patch.deviceLimit === 'number') {
    const have = await deviceCount(id)
    if (patch.deviceLimit < have) {
      throw createError({ statusCode: 409, statusMessage: 'device_limit_below_count', message: deviceLimitBelowCountText(have) })
    }
  }
  if (upd.name !== undefined) await assertNameFree(upd.name, id)
  if (upd.preferredExit) await assertKnownOutbound(`hy2-${upd.preferredExit}-direct`)
  upd.updatedAt = new Date()

  // Проверки выше уступают управление другим запросам: ограничения проверяет тот же SQL, что пишет.
  const conditions = [eq(clients.id, id)]
  if (upd.name !== undefined) {
    conditions.push(notExists(db.select({ id: clients.id }).from(clients).where(and(eq(clients.name, upd.name), ne(clients.id, id)))))
  }
  if (typeof patch.deviceLimit === 'number') {
    conditions.push(sql`(select count(*) from ${devices} where ${devices.clientId} = ${id}) <= ${patch.deviceLimit}`)
  }
  const queued = changesAccess ? await enqueueAccess({ clientId: id, actor, key, patch: upd, condition: and(...conditions) }) : null
  const row = queued ? queued.client : (await db.update(clients).set(upd).where(and(...conditions)).returning())[0]
  if (!row) {
    const [exists] = await db.select({ id: clients.id }).from(clients).where(eq(clients.id, id)).limit(1)
    if (!exists) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    if (upd.name !== undefined) await assertNameFree(upd.name, id)
    throw createError({ statusCode: 409, statusMessage: 'device_limit_below_count', message: 'Лимит не может быть меньше числа устройств клиента' })
  }
  if (patch.preferredExit !== undefined) await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))

  const object = `client:${row.id}`
  if (row.frozenManual !== before.frozenManual) {
    const n = await deviceCount(id)
    await recordEvent({
      type: row.frozenManual ? 'client_frozen' : 'client_unfrozen', level: 'info', object, actor,
      title: `${row.frozenManual ? 'Отключение запущено' : 'Включение запущено'} · ${row.name}`,
      detail: joinDetail(`${n} ${plural(n, 'device')}`, 'вручную', actor),
    })
  }
  if (time(before.expiresAt) !== time(row.expiresAt)) {
    const term = row.expiresAt ? `до ${msk(row.expiresAt)}` : 'бессрочно'
    await recordEvent({ type: 'client_term', level: 'info', object, actor, title: `Срок изменён · ${row.name}`, detail: joinDetail(term, actor) })
  }

  // Изменение уже записано: сбой очереди бота - предупреждение в ответе, не ошибка.
  const warnings: Array<string | undefined> = []
  const chat = before.tgChatId
  if (chat && patch.name !== undefined && row.name !== before.name) {
    warnings.push(await notifyWarning(notifyClient(chat, `✏️ Имя вашего профиля изменено на «${row.name}».`, row.name)))
  }
  const limit = chat && patch.deviceLimit !== undefined ? limitText(before.deviceLimit, row.deviceLimit) : ''
  if (chat && limit) warnings.push(await notifyWarning(notifyClient(chat, limit, row.name)))
  const warning = [...new Set(warnings.filter(Boolean))].join(' ')
  return { ...row, ...(queued ? { operation: queued.operation } : {}), ...(warning ? { warning } : {}) }
}
