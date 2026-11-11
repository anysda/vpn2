import { and, eq, ne, sql } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { z } from 'zod'
import type { ZodType } from 'zod'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { accessRequestKey, enqueueAccess, getAccess, retryAccess } from '../utils/access-operations'
import { accepted, admin, bot, idParam, notFound, recordIds } from '../utils/api'
import { botClientView, clientByChat } from '../utils/bot-api'
import { botSecretOrThrow, notifyClient, notifyWarning, sendBotEvent } from '../utils/bot-events'
import { ONLINE_MIN_BYTES, PERIOD_ACTIVE_MIN_BYTES, historyStartSec, periodStartSec, periodTotalsByClient, recentBytesByClient } from '../utils/client-activity'
import { ClientCreate, ClientPatch, createClient, updateClient } from '../utils/client-ops'
import { clientCard, clientList, deviceView } from '../utils/client-view'
import { BOT_ACTOR, actorOf } from '../utils/events'
import { generateClientPassword } from '../utils/password'
import { readBodyAs } from '../utils/validate'

// Привязка Telegram клиента к аккаунту по паролю (диплинк ?start=<password>).
const Link = z.object({ password: z.string().min(1), chatId: z.number().int(), username: z.string().optional() })

/** Правка клиента панелью и ботом: 202, когда изменение пошло операцией доступа. */
async function patchClient(event: H3Event, schema: ZodType<ClientPatch>, actor: string) {
  const result = await updateClient(idParam(event), await readBodyAs(event, schema), actor, accessRequestKey(event))
  if (result.operation) setResponseStatus(event, 202)
  return result
}

/** Удаление клиента идёт операцией доступа: сбой CA оставляет запись для повтора. */
async function deleteClient(event: H3Event, actor: string) {
  const { operation } = await enqueueAccess({ clientId: idParam(event), kind: 'delete', actor, key: accessRequestKey(event) })
  return accepted(event, { operation })
}

const retryOperation = async (event: H3Event) => accepted(event, { operation: await retryAccess(getRouterParam(event, 'id') ?? '') })

export default {
  /** Список клиентов: статус, число девайсов и суммарный трафик по девайсам. */
  'GET /clients': admin(() => clientList()),
  'POST /clients': admin(async (event, user) => createClient(await readBodyAs(event, ClientCreate), actorOf(user))),

  /**
   * ТОП клиентов по трафику за период (`?period=day|week`) и счётчики
   * активности. Сутки — с 00:00 МСК, неделя — 7 суток МСК. В `clients` — все
   * с ненулевым трафиком за период, по убыванию. `counts` от периода не
   * зависят: блок цифр в UI не прыгает при переключении.
   */
  'GET /clients/top': admin(async (event) => {
    const q = getQuery(event)
    if (q.period !== undefined && q.period !== 'day' && q.period !== 'week') {
      throw createError({ statusCode: 400, statusMessage: 'invalid_period' })
    }
    const period = q.period === 'week' ? 'week' : 'day'
    const since = { day: periodStartSec('day'), week: periodStartSec('week') }
    const [rows, day, week, recent, historyStart] = await Promise.all([
      useDb().select({ id: clients.id, name: clients.name }).from(clients),
      periodTotalsByClient(since.day),
      periodTotalsByClient(since.week),
      recentBytesByClient(),
      historyStartSec(),
    ])
    const totals = period === 'week' ? week : day
    const top = rows
      .map((c) => {
        const { rx = 0, tx = 0 } = totals.get(c.id) ?? {}
        return { id: c.id, name: c.name, rx, tx, total: rx + tx }
      })
      .filter(c => c.total > 0)
      .sort((a, b) => b.total - a.total)
    const activeIn = (m: Map<number, { rx: number, tx: number }>) =>
      [...m.values()].filter(t => t.rx + t.tx >= PERIOD_ACTIVE_MIN_BYTES).length
    return {
      period,
      since: since[period],
      historyStart,
      clients: top,
      counts: {
        total: rows.length,
        onlineNow: [...recent.values()].filter(b => b >= ONLINE_MIN_BYTES).length,
        activeDay: activeIn(day),
        activeWeek: activeIn(week),
      },
    }
  }),

  /**
   * Трафик по клиентам — сумма по их девайсам, по всем протоколам; наполняет
   * фоновый сборщик (utils/traffic-collector.ts). По номеру клиента:
   * rxBytes/txBytes за всё время, dayRx/dayTx с 00:00 МСК, recentBytes за
   * последние 5 минут, online — recentBytes ≥ ONLINE_MIN_BYTES.
   */
  'GET /clients/traffic': admin(async () => {
    const [rows, day, recent] = await Promise.all([
      useDb()
        .select({
          clientId: devices.clientId,
          rx: sql<number>`coalesce(sum(${devices.rxTotal}), 0)`,
          tx: sql<number>`coalesce(sum(${devices.txTotal}), 0)`,
        })
        .from(devices)
        .groupBy(devices.clientId),
      periodTotalsByClient(periodStartSec('day')),
      recentBytesByClient(),
    ])
    return Object.fromEntries(rows.map((r) => {
      const recentBytes = recent.get(r.clientId) ?? 0
      return [r.clientId, {
        rxBytes: Number(r.rx),
        txBytes: Number(r.tx),
        dayRx: day.get(r.clientId)?.rx ?? 0,
        dayTx: day.get(r.clientId)?.tx ?? 0,
        recentBytes,
        online: recentBytes >= ONLINE_MIN_BYTES,
      }]
    }))
  }),

  /** Детали клиента + его девайсы — для модалки. */
  'GET /clients/:id': admin(async (event) => {
    const { client, devices, operation, status, tgLinked } = await clientCard(event)
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
      rxTotal: devices.reduce((sum, d) => sum + d.rxTotal, 0),
      txTotal: devices.reduce((sum, d) => sum + d.txTotal, 0),
      devices: devices.map(d => ({ ...deviceView(client, d), createdAt: d.createdAt, rxTotal: d.rxTotal, txTotal: d.txTotal })),
    }
  }),
  'PATCH /clients/:id': admin((event, user) => patchClient(event, ClientPatch, actorOf(user))),
  'DELETE /clients/:id': admin((event, user) => deleteClient(event, actorOf(user))),

  'POST /clients/:id/reissue': admin(async (event, user) => {
    const { id } = recordIds(event, 'id')
    const { operation } = await enqueueAccess({ clientId: id, kind: 'reissue', actor: actorOf(user), key: accessRequestKey(event) })
    return accepted(event, { operation })
  }),

  /** Отправить пароль клиента в Telegram. Задел под будущую фичу. */
  'POST /clients/:id/send-password-to-tg': admin(async (event) => {
    const id = idParam(event)
    botSecretOrThrow()
    const [client] = await useDb().select().from(clients).where(eq(clients.id, id)).limit(1)
    if (!client) throw notFound()
    await sendBotEvent({ type: 'client_send_password', name: client.name, password: client.password })
    return { ok: true }
  }),

  /**
   * Отвязать Telegram клиента от бота («Отозвать доступ к боту»).
   * Пароль перевыпускаем: инвайт-ссылка — это `?start=<password>`, и без
   * смены пароля старая ссылка позволила бы привязаться к боту заново.
   *
   * Отзыв фиксируется первым, уведомление уходит после — на чат из строки до
   * очистки. Очередь бота может быть полна или бот не настроен: доступ всё
   * равно отозван, сбой доставки возвращается предупреждением.
   */
  'POST /clients/:id/unlink': admin(async (event) => {
    const id = idParam(event)
    const before = await useDb().transaction(async (tx) => {
      const [client] = await tx.select({ tgChatId: clients.tgChatId, name: clients.name }).from(clients)
        .where(eq(clients.id, id)).limit(1)
      if (!client) throw notFound()
      await tx.update(clients)
        .set({ tgChatId: null, tgUsername: null, password: generateClientPassword(), updatedAt: new Date() })
        .where(eq(clients.id, id))
      return client
    }, { behavior: 'immediate' })
    if (!before.tgChatId) return { ok: true }
    const warning = await notifyWarning(notifyClient(before.tgChatId, '🔒 Ваш доступ к этому боту отозван администратором.', before.name))
    return { ok: true, ...(warning ? { warning } : {}) }
  }),

  'GET /access-operations/:id': admin(event => getAccess(getRouterParam(event, 'id') ?? '')),
  'POST /access-operations/:id/retry': admin(retryOperation),

  // Админский чат бота: те же операции, что в панели, со своим набором полей.
  'GET /bot/admin/clients': bot(async () => (await clientList()).map(c => ({
    id: c.id,
    name: c.name,
    status: c.status,
    accessStatus: c.accessStatus,
    deviceCount: c.deviceCount,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    tgLinked: c.tgLinked,
  }))),
  // Создание из бота — имя + фильтрация; срок и лимит админ задаёт потом карточкой (PATCH).
  'POST /bot/admin/clients': bot(async event => createClient(await readBodyAs(event, ClientCreate.pick({ name: true, filterTraffic: true })), BOT_ACTOR)),
  /** Карточка клиента для админского чата бота. password — для диплинка-приглашения. */
  'GET /bot/admin/clients/:id': bot(async (event) => {
    const { client: c, devices, operation, accessStatus, status, tgLinked } = await clientCard(event)
    return {
      operation,
      accessStatus,
      id: c.id,
      name: c.name,
      status,
      frozenManual: c.frozenManual,
      filterTraffic: c.filterTraffic,
      deviceCount: devices.length,
      deviceLimit: c.deviceLimit,
      expiresAt: c.expiresAt,
      tgLinked,
      password: c.password,
    }
  }),
  'PATCH /bot/admin/clients/:id': bot(event => patchClient(event, ClientPatch.omit({ preferredExit: true }), BOT_ACTOR)),
  'DELETE /bot/admin/clients/:id': bot(event => deleteClient(event, BOT_ACTOR)),
  'POST /bot/admin/access-operations/:id/retry': bot(retryOperation),

  // Резолв клиента по Telegram-чату — бот зовёт на каждое взаимодействие.
  'GET /bot/client': bot(async (event) => {
    const chatId = Number(getQuery(event).chatId)
    if (!Number.isFinite(chatId)) throw createError({ statusCode: 400, statusMessage: 'invalid_chat_id' })
    return botClientView((await clientByChat(chatId)).id)
  }),

  'POST /bot/link': bot(async (event) => {
    const body = await readBodyAs(event, Link)
    // Пароль перепроверяется тем же UPDATE, что пишет привязку: отзыв между
    // поиском и записью меняет пароль, и запись не находит строку.
    const db = useDb()
    const [found] = await db.select({ id: clients.id }).from(clients)
      .where(eq(clients.password, body.password)).limit(1)
    if (!found) throw createError({ statusCode: 404, statusMessage: 'invalid_password' })
    const clientId = await db.transaction(async (tx) => {
      const [linked] = await tx.update(clients)
        .set({ tgChatId: body.chatId, tgUsername: body.username ?? null, updatedAt: new Date() })
        .where(and(eq(clients.id, found.id), eq(clients.password, body.password)))
        .returning({ id: clients.id })
      if (!linked) throw createError({ statusCode: 404, statusMessage: 'invalid_password' })
      // Этот Telegram мог быть привязан к другому клиенту — отвязываем там.
      await tx.update(clients).set({ tgChatId: null })
        .where(and(eq(clients.tgChatId, body.chatId), ne(clients.id, linked.id)))
      return linked.id
    }, { behavior: 'immediate' })
    return botClientView(clientId)
  }),
}
