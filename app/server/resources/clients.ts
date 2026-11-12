import { createClient, updateClient } from '../utils/client-ops'
import { and, eq, ne } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { z } from 'zod'
import type { ZodType } from 'zod'
import { useDb } from '../database/client'
import { clients } from '../database/schema'

/** Привязка Telegram по паролю клиента (диплинк ?start=<password>). */
const Link = z.object({ password: z.string().min(1), chatId: z.number().int(), username: z.string().optional() })
const invalidPassword = () => createError({ statusCode: 404, statusMessage: 'invalid_password' })
type Totals = Map<number, { rx: number, tx: number }>

/** Правка клиента панелью и ботом: 202, когда изменение пошло операцией доступа. */
async function patchClient(event: H3Event, schema: ZodType<ClientPatch>, actor: string) {
  const result = await updateClient(idParam(event), await readBodyAs(event, schema), actor, accessRequestKey(event))
  if (result.operation) setResponseStatus(event, 202)
  return result
}

/** Операция доступа над клиентом целиком; сбой CA оставляет запись для повтора. */
async function clientAccess(event: H3Event, kind: 'delete' | 'reissue', actor: string) {
  const id = kind === 'delete' ? idParam(event) : recordIds(event, 'id').id
  return accepted(event, { operation: (await enqueueAccess({ clientId: id, kind, actor, key: accessRequestKey(event) })).operation })
}

const retryOperation = async (event: H3Event) => accepted(event, { operation: await retryAccess(getRouterParam(event, 'id') ?? '') })
const activeIn = (totals: Totals) => [...totals.values()].filter(t => t.rx + t.tx >= PERIOD_ACTIVE_MIN_BYTES).length

export default {
  'GET /clients': admin(() => clientList()),
  'POST /clients': admin(async (event, user) => createClient(await readBodyAs(event, ClientCreate), actorOf(user))),

  /**
   * ТОП клиентов по трафику за период (`?period=day|week`, сутки и неделя по МСК) и счётчики
   * активности. `counts` от периода не зависят: блок цифр в UI не прыгает при переключении.
   */
  'GET /clients/top': admin(async (event) => {
    const q = getQuery(event)
    if (q.period !== undefined && q.period !== 'day' && q.period !== 'week') throw createError({ statusCode: 400, statusMessage: 'invalid_period' })
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
    const top = rows.map((c) => {
      const { rx = 0, tx = 0 } = totals.get(c.id) ?? {}
      return { id: c.id, name: c.name, rx, tx, total: rx + tx }
    }).filter(c => c.total > 0).sort((a, b) => b.total - a.total)
    const counts = {
      total: rows.length,
      onlineNow: [...recent.values()].filter(b => b >= ONLINE_MIN_BYTES).length,
      activeDay: activeIn(day),
      activeWeek: activeIn(week),
    }
    return { period, since: since[period], historyStart, clients: top, counts }
  }),

  /**
   * Трафик по клиентам - сумма по девайсам и протоколам: за всё время, с 00:00 МСК
   * и за последние 5 минут; online - recentBytes ≥ ONLINE_MIN_BYTES.
   */
  'GET /clients/traffic': admin(async () => {
    const [totals, day, recent] = await Promise.all([
      deviceTotalsByClient(),
      periodTotalsByClient(periodStartSec('day')),
      recentBytesByClient(),
    ])
    return Object.fromEntries([...totals].map(([clientId, t]) => {
      const recentBytes = recent.get(clientId) ?? 0
      return [clientId, {
        rxBytes: t.rx,
        txBytes: t.tx,
        dayRx: day.get(clientId)?.rx ?? 0,
        dayTx: day.get(clientId)?.tx ?? 0,
        recentBytes,
        online: recentBytes >= ONLINE_MIN_BYTES,
      }]
    }))
  }),

  'GET /clients/:id': admin(async (event) => {
    const { client: c, devices, operation, status, tgLinked } = await clientCard(idParam(event))
    const sum = (key: 'rxTotal' | 'txTotal') => devices.reduce((total, d) => total + d[key], 0)
    return {
      operation,
      deleting: c.deleting,
      id: c.id,
      name: c.name,
      filterTraffic: c.filterTraffic,
      expiresAt: c.expiresAt,
      deviceLimit: c.deviceLimit,
      frozenManual: c.frozenManual,
      createdAt: c.createdAt,
      status,
      tgLinked,
      tgUsername: c.tgUsername,
      preferredExit: c.preferredExit,
      rxTotal: sum('rxTotal'),
      txTotal: sum('txTotal'),
      devices: devices.map(d => ({ ...deviceView(c, d), createdAt: d.createdAt, rxTotal: d.rxTotal, txTotal: d.txTotal })),
    }
  }),
  'PATCH /clients/:id': admin((event, user) => patchClient(event, ClientPatch, actorOf(user))),
  'DELETE /clients/:id': admin((event, user) => clientAccess(event, 'delete', actorOf(user))),
  'POST /clients/:id/reissue': admin((event, user) => clientAccess(event, 'reissue', actorOf(user))),

  'POST /clients/:id/send-password-to-tg': admin(async (event) => {
    const id = idParam(event)
    botSecretOrThrow()
    const client = await clientOr404(id)
    await sendBotEvent({ type: 'client_send_password', name: client.name, password: client.password })
    return { ok: true }
  }),

  /**
   * «Отозвать доступ к боту». Пароль перевыпускается: инвайт `?start=<password>` иначе
   * привязал бы бота заново. Сбой доставки уведомления - предупреждение, доступ уже отозван.
   */
  'POST /clients/:id/unlink': admin(async (event) => {
    const id = idParam(event)
    const before = await useDb().transaction(async (tx) => {
      const [client] = await tx.select({ tgChatId: clients.tgChatId, name: clients.name }).from(clients).where(eq(clients.id, id)).limit(1)
      if (!client) throw notFound()
      await tx.update(clients).set({ tgChatId: null, tgUsername: null, password: generateClientPassword(), updatedAt: new Date() })
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
  // Из бота - имя и фильтрация; срок и лимит админ задаёт потом карточкой.
  'POST /bot/admin/clients': bot(async event => createClient(await readBodyAs(event, ClientCreate.pick({ name: true, filterTraffic: true })), BOT_ACTOR)),
  /** password - для диплинка-приглашения. */
  'GET /bot/admin/clients/:id': bot(async (event) => {
    const { client: c, devices, operation, accessStatus, status, tgLinked } = await clientCard(idParam(event))
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
  'DELETE /bot/admin/clients/:id': bot(event => clientAccess(event, 'delete', BOT_ACTOR)),
  'POST /bot/admin/access-operations/:id/retry': bot(retryOperation),

  'GET /bot/client': bot(async (event) => {
    const chatId = Number(getQuery(event).chatId)
    if (!Number.isFinite(chatId)) throw createError({ statusCode: 400, statusMessage: 'invalid_chat_id' })
    return botClientView((await clientByChat(chatId)).id)
  }),

  /** Пароль перепроверяется тем же UPDATE, что пишет привязку: отзыв между поиском и записью меняет пароль. */
  'POST /bot/link': bot(async (event) => {
    const body = await readBodyAs(event, Link)
    const db = useDb()
    const [found] = await db.select({ id: clients.id }).from(clients).where(eq(clients.password, body.password)).limit(1)
    if (!found) throw invalidPassword()
    const clientId = await db.transaction(async (tx) => {
      const [linked] = await tx.update(clients)
        .set({ tgChatId: body.chatId, tgUsername: body.username ?? null, updatedAt: new Date() })
        .where(and(eq(clients.id, found.id), eq(clients.password, body.password)))
        .returning({ id: clients.id })
      if (!linked) throw invalidPassword()
      // Этот Telegram мог быть привязан к другому клиенту - там отвязывается.
      await tx.update(clients).set({ tgChatId: null }).where(and(eq(clients.tgChatId, body.chatId), ne(clients.id, linked.id)))
      return linked.id
    }, { behavior: 'immediate' })
    return botClientView(clientId)
  }),
}
