import { and, count, eq, ne, notExists, sql } from 'drizzle-orm'
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
import { pluralRu } from '../../shared/plural'

/**
 * Общие операции над клиентами — используются и панелью (session-auth),
 * и ботом (Bearer-auth админский чат). Здесь же — валидация и уведомления
 * привязанному клиенту в Telegram.
 */

export interface ClientPatch {
  name?: string
  expiresAt?: string | null
  deviceLimit?: number | null
  frozenManual?: boolean
  // тег узла экзита или null — «авто»
  preferredExit?: string | null
}

export interface ClientCreateInput {
  name: string
  filterTraffic?: boolean
  expiresAt?: string | null
  deviceLimit?: number | null
}

const devicesText = (n: number) => `${n} ${pluralRu(n, ['устройство', 'устройства', 'устройств'])}`

/** Срок для журнала — дата по Москве, как её видит админ. */
function termText(expiresAt: Date | null): string {
  return expiresAt ? `до ${expiresAt.toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' })}` : 'бессрочно'
}

/** Срок действия нельзя ставить раньше завтрашней даты. */
function assertExpiryAllowed(expiresAt: string | null | undefined): void {
  if (expiresAt && new Date(expiresAt).getTime() < minExpiryMs()) {
    throw createError({
      statusCode: 422,
      statusMessage: 'invalid_expiry', message: 'Срок действия не может быть раньше завтрашней даты',
    })
  }
}

/** Создать клиента (имя + опции). Пароль генерится автоматически. */
export async function createClient(input: ClientCreateInput, actor: string): Promise<Client & { warning?: string }> {
  const db = useDb()
  assertExpiryAllowed(input.expiresAt)

  const name = input.name.trim()
  const dup = await db.select({ id: clients.id }).from(clients).where(eq(clients.name, name)).limit(1)
  if (dup.length > 0) {
    throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${name}» уже существует` })
  }

  const values: typeof clients.$inferInsert = {
    name,
    filterTraffic: input.filterTraffic ?? true,
    password: generateClientPassword(),
  }
  if (input.expiresAt !== undefined) values.expiresAt = input.expiresAt ? new Date(input.expiresAt) : null
  if (input.deviceLimit !== undefined) values.deviceLimit = input.deviceLimit

  const [row] = await db.insert(clients).values(values).returning()
  if (!row) throw createError({ statusCode: 500, statusMessage: 'insert_failed' })
  // Клиент уже создан: сбой очереди бота - предупреждение в ответе, не ошибка.
  const warning = await notifyWarning(notifyBot('client_created', { name: row.name }), 'Уведомление о клиенте не отправлено')
  await recordEvent({ type: 'client_created', level: 'info', object: `client:${row.id}`, actor, title: `Клиент создан · ${row.name}`, detail: actor })
  return { ...row, ...(warning ? { warning } : {}) }
}

/** Обновить клиента: валидация, апдейт, ресинк WG/OVPN, уведомления клиенту. */
export async function updateClient(id: number, patch: ClientPatch, actor: string, key?: string): Promise<Partial<Client> & { id: number, operation?: AccessOperation, warning?: string }> {
  const db = useDb()
  if (key && (patch.frozenManual !== undefined || patch.expiresAt !== undefined)) {
    const normalized = { ...patch,
      ...(patch.name !== undefined ? { name: patch.name.trim() } : {}),
      ...(patch.expiresAt !== undefined ? { expiresAt: patch.expiresAt ? new Date(patch.expiresAt) : null } : {}),
      ...(patch.preferredExit !== undefined ? { preferredExit: patch.preferredExit || null } : {}),
    } as Partial<typeof clients.$inferInsert>
    const replay = await replayAccess(key, { clientId: id, patch: normalized })
    if (replay) return replay
  }
  assertExpiryAllowed(patch.expiresAt)

  const [before] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Лимит девайсов нельзя опустить ниже текущего числа устройств клиента.
  if (typeof patch.deviceLimit === 'number') {
    const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))
    const have = Number(dc?.n ?? 0)
    if (patch.deviceLimit < have) {
      throw createError({
        statusCode: 409,
        statusMessage: 'device_limit_below_count', message: `У клиента уже ${have} ${pluralRu(have, ['устройство', 'устройства', 'устройств'])} — лимит не может быть меньше`,
      })
    }
  }

  const upd: Partial<typeof clients.$inferInsert> = { updatedAt: new Date() }
  if (patch.name !== undefined) {
    const name = patch.name.trim()
    const dup = await db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.name, name), ne(clients.id, id)))
      .limit(1)
    if (dup.length > 0) {
      throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${name}» уже существует` })
    }
    upd.name = name
  }
  if (patch.expiresAt !== undefined) upd.expiresAt = patch.expiresAt ? new Date(patch.expiresAt) : null
  if (patch.deviceLimit !== undefined) upd.deviceLimit = patch.deviceLimit
  if (patch.frozenManual !== undefined) upd.frozenManual = patch.frozenManual
  if (patch.preferredExit !== undefined) {
    if (patch.preferredExit) await assertKnownOutbound(`hy2-${patch.preferredExit}-direct`)
    upd.preferredExit = patch.preferredExit || null
  }

  // Проверка sing-box выше уступает управление другим запросам. Имя могло
  // стать занятым, а число устройств вырасти: проверяем ограничения в том
  // же SQL, который записывает изменения, без отдельной write-транзакции.
  const conditions = [eq(clients.id, id)]
  if (upd.name !== undefined) {
    conditions.push(notExists(db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.name, upd.name), ne(clients.id, id)))))
  }
  if (typeof patch.deviceLimit === 'number') {
    conditions.push(sql`(select count(*) from ${devices} where ${devices.clientId} = ${id}) <= ${patch.deviceLimit}`)
  }
  const changesAccess = patch.frozenManual !== undefined || patch.expiresAt !== undefined
  const queued = changesAccess ? await enqueueAccess({ clientId: id, actor, key, patch: upd, condition: and(...conditions) }) : null
  const row = queued ? queued.client : (await db.update(clients).set(upd).where(and(...conditions)).returning())[0]
  if (!row) {
    const [exists] = await db.select({ id: clients.id }).from(clients).where(eq(clients.id, id)).limit(1)
    if (!exists) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    if (upd.name !== undefined) {
      const dup = await db.select({ id: clients.id }).from(clients)
        .where(and(eq(clients.name, upd.name), ne(clients.id, id))).limit(1)
      if (dup.length) {
        throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${upd.name}» уже существует` })
      }
    }
    throw createError({
      statusCode: 409,
      statusMessage: 'device_limit_below_count', message: 'Лимит не может быть меньше числа устройств клиента',
    })
  }

  if (patch.preferredExit !== undefined) {
    await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))
  }

  const object = `client:${row.id}`
  if (row.frozenManual !== before.frozenManual) {
    const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))
    await recordEvent({
      type: row.frozenManual ? 'client_frozen' : 'client_unfrozen', level: 'info', object, actor,
      title: `${row.frozenManual ? 'Отключение запущено' : 'Включение запущено'} · ${row.name}`,
      detail: joinDetail(devicesText(Number(dc?.n ?? 0)), 'вручную', actor),
    })
  }
  if ((before.expiresAt?.getTime() ?? null) !== (row.expiresAt?.getTime() ?? null)) {
    await recordEvent({ type: 'client_term', level: 'info', object, actor, title: `Срок изменён · ${row.name}`, detail: joinDetail(termText(row.expiresAt), actor) })
  }

  // Уведомления привязанному клиенту об изменениях аккаунта. Изменение уже
  // записано: сбой очереди бота - предупреждение в ответе, не ошибка.
  const chat = before.tgChatId
  const warnings: Array<string | undefined> = []
  if (chat) {
    if (patch.name !== undefined && row.name !== before.name) {
      warnings.push(await notifyWarning(notifyClient(chat, `✏️ Имя вашего профиля изменено на «${row.name}».`, row.name)))
    }
    if (patch.deviceLimit !== undefined) {
      const rank = (v: number | null) => (v === null ? Infinity : v)
      const oldR = rank(before.deviceLimit)
      const newR = rank(row.deviceLimit)
      if (newR > oldR) {
        warnings.push(await notifyWarning(notifyClient(chat, row.deviceLimit === null
          ? '📈 Лимит устройств снят — теперь без ограничений.'
          : `📈 Ваш лимит устройств повышен до ${row.deviceLimit}.`, row.name)))
      }
      else if (newR < oldR) {
        warnings.push(await notifyWarning(notifyClient(chat, `📉 Ваш лимит устройств понижен до ${row.deviceLimit}.`, row.name)))
      }
    }
  }
  const warning = [...new Set(warnings.filter(Boolean))].join(' ')

  return { ...row, ...(queued ? { operation: queued.operation } : {}), ...(warning ? { warning } : {}) }
}

/** Удалить клиента: каскадно девайсы, ресинк WG, отзыв OVPN-сертификатов. */
export async function deleteClient(id: number, actor: string, key?: string) {
  const { operation } = await enqueueAccess({ clientId: id, kind: 'delete', actor, key })
  return { operation }
}

/** Общее удаление устройства из панели и бота; ошибка CA оставляет запись для повтора. */
export async function deleteDevice(clientId: number, deviceId: number, actor: string, key?: string) {
  const { operation } = await enqueueAccess({ clientId, deviceId, kind: 'delete', actor, key })
  return { operation }
}
