import type { H3Event } from 'h3'
import { and, count, eq, gte, isNotNull } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../database/client'
import { accounts, botOutbox, clients, users } from '../database/schema'
import { admin, bot } from '../utils/api'
import { resetAuth, revokeDisallowedSsoSessions, useAuth } from '../utils/auth'
import { botDeliveries, notifyBackupFailed, notifyBot } from '../utils/bot-events'
import { actorOf, joinDetail, recordEvent } from '../utils/events'
import {
  defaultRedirectUrl, discoveryFrom, maskSecret, parseAllowedSubs, probeDiscovery, readSsoStored, SSO_PROVIDER, ssoSettings, writeSsoStored,
  type SsoStored,
} from '../utils/sso'
import { botConfigured, botHealth, maskToken, readTelegramRuntime, writeTelegramRuntime } from '../utils/telegram-config'
import { readBodyAs } from '../utils/validate'

const DAY = 24 * 60 * 60 * 1000

// Тревоги извне: от бота (Telegram отверг сообщение) и от скрипта бэкапа этой ноды.
const BotEvent = z.discriminatedUnion('type', [
  z.object({ type: z.literal('telegram_rejected'), status: z.number().int().min(100).max(599), recipient: z.enum(['admin', 'client']) }),
  z.object({ type: z.literal('backup_failed'), detail: z.string().trim().max(200).default('') }),
  z.object({ type: z.literal('backup_warning'), detail: z.string().trim().max(200).default('') }),
])

const Telegram = z.object({
  bot_token: z.string().optional(),
  // Только числовой id чата (у групп отрицательный); пустая строка - взять из env.
  chat_id: z.union([z.string().trim().regex(/^(-?\d+)?$/), z.number().int()]).optional(),
  admin_username: z.string().optional(),
})

const Sso = z.object({
  enabled: z.boolean(),
  authentikUrl: z.string().trim(),
  appSlug: z.string().trim(),
  clientId: z.string().trim(),
  // Пусто - оставить сохранённый: секрет в браузер не отдаётся.
  clientSecret: z.string().trim().default(''),
  allowedSubs: z.string().trim(),
  label: z.string().trim().max(40),
  autoRedirect: z.boolean(),
  passwordLogin: z.boolean(),
  redirectUrl: z.string().trim(),
})
/** То, от чего зависит сам вход через Authentik. */
const PROTOCOL: (keyof SsoStored)[] = ['enabled', 'authentikUrl', 'appSlug', 'clientId', 'clientSecret', 'allowedSubs', 'redirectUrl']

function reject(code: string, message: string): never {
  throw createError({ statusCode: 400, statusMessage: code, message })
}
const countOf = async (query: Promise<{ n: number }[]>) => (await query)[0]?.n ?? 0
const session = (event: H3Event) => useAuth().api.getSession({ headers: event.headers })

/** Включённый SSO должен пускать хоть кого-то из учёток панели. */
async function checkSsoEnabled(next: SsoStored, allowed: Map<string, string | null>) {
  if (!/^https?:\/\/[^/\s]+/.test(discoveryFrom(next.authentikUrl, next.appSlug))) reject('sso_bad_url', 'Укажите адрес Authentik: https://… или discovery URL')
  if (!next.clientId) reject('sso_no_client_id', 'Укажите client_id провайдера')
  if (!next.clientSecret.trim()) reject('sso_no_client_secret', 'Укажите client_secret провайдера')
  // Куки state/PKCE с флагом Secure: по HTTP возврат из Authentik не сойдётся.
  if (!/^https:\/\/[^/\s]+/.test(next.redirectUrl)) {
    reject('sso_bad_redirect', 'Адрес возврата нужен https://<домен панели>/auth/authentik: вход через Authentik работает только по HTTPS')
  }
  if (allowed.size === 0) reject('sso_no_allowed_subs', 'Список «кого пускать» пуст — через Authentik не войдёт никто')
  const db = useDb()
  for (const login of new Set(allowed.values())) {
    if (login === null) continue
    const [u] = await db.select({ id: users.id }).from(users).where(eq(users.username, login)).limit(1)
    if (!u) reject('sso_no_local_user', `В панели нет учётки «${login}»`)
  }
  if ([...allowed.values()].includes(null) && (await db.select({ id: users.id }).from(users).limit(2)).length !== 1) {
    reject('sso_ambiguous', 'Учёток в панели несколько — укажите логин: <sub>:<логин>')
  }
}

/** Пароль выключается, только когда вход через Authentik доказан этой же сессией и SSO не меняется. */
async function checkPasswordOff(next: SsoStored, cur: SsoStored, allowed: Map<string, string | null>, s: NonNullable<Awaited<ReturnType<typeof session>>>) {
  if (!next.enabled) reject('sso_password_needed', 'Без SSO парольный вход выключить нельзя')
  if ((s.session as { loginMethod?: string | null }).loginMethod !== 'sso' || !cur.enabled) {
    reject('sso_not_proven', 'Пароль выключается только после входа через Authentik: сохраните SSO, выйдите и войдите через него')
  }
  if (PROTOCOL.some(k => next[k] !== cur[k])) reject('sso_not_proven', 'Пароль выключается отдельным сохранением, без правки остальных полей SSO')
  const [link] = await useDb().select({ sub: accounts.accountId }).from(accounts)
    .where(and(eq(accounts.userId, Number(s.user.id)), eq(accounts.providerId, SSO_PROVIDER))).limit(1)
  if (!link || !allowed.has(link.sub)) reject('sso_not_proven', 'Вашей учётки Authentik нет в списке «кого пускать»')
  const probe = await probeDiscovery(discoveryFrom(next.authentikUrl, next.appSlug))
  if (!probe.ok) reject('sso_discovery_failed', probe.message)
}

const backupEvents = {
  backup_warning: { level: 'warn', title: 'Бэкап сохранён с предупреждением' },
  backup_failed: { level: 'alarm', title: 'Бэкап не удался' },
} as const

export default {
  'POST /bot/events': bot(async (event) => {
    const body = await readBodyAs(event, BotEvent)
    if (body.type === 'telegram_rejected') {
      await recordEvent({
        type: 'telegram_rejected', level: 'alarm', object: 'bot', title: 'Telegram не принял сообщение бота',
        detail: `${body.recipient === 'client' ? 'клиенту' : 'админу'} · код ${body.status}`,
      })
      return { ok: true }
    }
    await recordEvent({ type: body.type, object: 'backup', detail: body.detail, ...backupEvents[body.type] })
    // Отказ бэкапа ещё и админу в Telegram: в панель он может не заглядывать.
    if (body.type === 'backup_failed') await notifyBackupFailed(body.detail)
    return { ok: true }
  }),

  // Фоновый опрос может пересечься с выходом или сменой сессии при 2FA: без сессии пустой список.
  'GET /admin/bot-deliveries': async (event: H3Event) => {
    setHeader(event, 'Cache-Control', 'no-store')
    return await session(event) ? botDeliveries() : []
  },

  'POST /admin/broadcast': admin(async (event) => {
    const { text } = await readBodyAs(event, z.object({ text: z.string().trim().min(1).max(4096) }))
    if (!botConfigured()) {
      throw createError({ statusCode: 409, statusMessage: 'bot_not_configured', message: 'Telegram-бот не настроен. Рассылка не отправлена.' })
    }
    const rows = await useDb().select({ chatId: clients.tgChatId }).from(clients).where(isNotNull(clients.tgChatId))
    const chatIds = [...new Set(rows.map(row => row.chatId!))]
    const delivery = chatIds.length ? await notifyBot('broadcast', { chatIds, text }, 'Рассылка') : null
    return { recipients: chatIds.length, delivery }
  }),

  'GET /admin/telegram': admin(async () => {
    const cfg = readTelegramRuntime()
    const db = useDb()
    const [health, all, subscribed, queued, delivered] = await Promise.all([
      botHealth(),
      countOf(db.select({ n: count() }).from(clients)),
      countOf(db.select({ n: count() }).from(clients).where(isNotNull(clients.tgChatId))),
      countOf(db.select({ n: count() }).from(botOutbox).where(eq(botOutbox.status, 'queued'))),
      // Очередь панели хранит записи те же 24 часа.
      countOf(db.select({ n: count() }).from(botOutbox).where(and(eq(botOutbox.status, 'accepted'), gte(botOutbox.created, Date.now() - DAY)))),
    ])
    return {
      configured: !!cfg.bot_token && !!cfg.chat_id,
      running: health.running,
      username: health.username,
      bot_token_masked: maskToken(cfg.bot_token),
      chat_id: cfg.chat_id,
      admin_username: cfg.admin_username,
      clients_total: all,
      clients_subscribed: subscribed,
      queued,
      delivered_day: delivered,
    }
  }),
  'PUT /admin/telegram': admin(async (event, user) => {
    const body = await readBodyAs(event, Telegram)
    const cur = readTelegramRuntime()
    const next = {
      bot_token: body.bot_token ?? cur.bot_token,
      chat_id: body.chat_id !== undefined ? String(body.chat_id) : cur.chat_id,
      // Никнейм без ведущего @: бот добавит его при показе.
      admin_username: body.admin_username?.trim().replace(/^@+/, '') ?? cur.admin_username,
    }
    writeTelegramRuntime(next)
    useLogger().info({ chat_id: next.chat_id }, 'telegram runtime updated')
    const changed = [
      next.bot_token !== cur.bot_token && 'токен бота',
      next.chat_id !== cur.chat_id && 'чат админа',
      next.admin_username !== cur.admin_username && 'ник админа',
    ].filter(Boolean)
    if (changed.length) {
      await recordEvent({
        type: 'telegram_changed', level: 'alarm', object: 'settings:telegram', actor: actorOf(user),
        title: 'Изменены настройки Telegram', detail: joinDetail(changed.join(', '), actorOf(user)),
      })
    }
    return { ok: true }
  }),

  // Страница входа без сессии: показывать ли кнопку Authentik.
  'GET /sso': () => {
    const { enabled, autoRedirect, label } = ssoSettings()
    return { enabled, autoRedirect, label }
  },
  'GET /admin/sso': admin((event) => {
    const { stored, fromPanel } = readSsoStored()
    const { clientSecret, ...rest } = stored
    return {
      ...rest,
      clientSecretMasked: maskSecret(clientSecret),
      hasSecret: !!clientSecret,
      redirectUrl: stored.redirectUrl || defaultRedirectUrl(event),
      discoveryUrl: discoveryFrom(stored.authentikUrl, stored.appSlug),
      source: fromPanel ? 'panel' : 'config',
    }
  }),
  /** «Проверить»: discovery по введённым, ещё не сохранённым полям. */
  'POST /admin/sso/check': admin(async (event) => {
    const body = await readBodyAs(event, z.object({ authentikUrl: z.string().trim(), appSlug: z.string().trim().default('') }))
    const url = discoveryFrom(body.authentikUrl, body.appSlug)
    if (!/^https?:\/\//.test(url)) reject('sso_bad_url', 'Адрес Authentik должен начинаться с https://')
    const r = await probeDiscovery(url)
    if (!r.ok) reject('sso_discovery_failed', r.message)
    return { ok: true, issuer: r.issuer, discoveryUrl: url }
  }),
  /** Сохранение SSO из профиля: после него в панель должен войти хоть кто-то. */
  'PUT /admin/sso': async (event: H3Event) => {
    const s = await session(event)
    if (!s) throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
    const body = await readBodyAs(event, Sso)
    const { stored: cur } = readSsoStored()
    const next: SsoStored = { ...body, label: body.label || 'Authentik', clientSecret: body.clientSecret || cur.clientSecret }
    const allowed = parseAllowedSubs(next.allowedSubs)
    if (next.enabled) await checkSsoEnabled(next, allowed)
    if (!next.passwordLogin) await checkPasswordOff(next, cur, allowed, s)
    writeSsoStored(next)
    const revoked = await revokeDisallowedSsoSessions()
    if ((Object.keys(next) as (keyof SsoStored)[]).some(k => next[k] !== cur[k])) {
      const actor = actorOf({ username: String((s.user as { username?: string }).username ?? '') })
      await recordEvent({
        type: 'sso_changed', level: 'alarm', object: 'settings:sso', actor,
        title: 'Изменены настройки SSO', detail: joinDetail(next.enabled ? 'вход через Authentik включён' : 'вход через Authentik выключен', actor),
      })
    }
    // Провайдер better-auth собирается из настроек при создании экземпляра.
    resetAuth()
    useLogger().info({ enabled: next.enabled, clientId: next.clientId, passwordLogin: next.passwordLogin, revokedSessions: revoked }, 'sso settings updated from profile')
    return { ok: true }
  },
}
