import { and, eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { accounts, users } from '../../database/schema'
import { resetAuth, revokeDisallowedSsoSessions, useAuth } from '../../utils/auth'
import { actorOf, joinDetail, recordEvent } from '../../utils/events'
import {
  discoveryFrom, parseAllowedSubs, probeDiscovery, readSsoStored, SSO_PROVIDER, writeSsoStored,
  type SsoStored,
} from '../../utils/sso'
import { readBodyAs } from '../../utils/validate'

const Body = z.object({
  enabled: z.boolean(),
  authentikUrl: z.string().trim(),
  appSlug: z.string().trim(),
  clientId: z.string().trim(),
  // Пусто — оставить прежний: целиком секрет в браузер не отдаётся.
  clientSecret: z.string().trim().default(''),
  allowedSubs: z.string().trim(),
  label: z.string().trim().max(40),
  autoRedirect: z.boolean(),
  passwordLogin: z.boolean(),
  redirectUrl: z.string().trim(),
})

function reject(code: string, message: string): never {
  throw createError({ statusCode: 400, statusMessage: code, message })
}

const isHttpUrl = (s: string) => /^https?:\/\/[^/\s]+/.test(s)

/** То, от чего зависит сам вход через Authentik. */
const PROTOCOL: (keyof SsoStored)[] = ['enabled', 'authentikUrl', 'appSlug', 'clientId', 'clientSecret', 'allowedSubs', 'redirectUrl']

/**
 * Сохранение SSO из профиля. Правило одно: после сохранения в панель
 * должен войти хоть кто-то. Отсюда проверки как в config2env.py и запрет
 * выключить пароль, пока вход через Authentik не доказан этой же сессией.
 */
export default defineEventHandler(async (event) => {
  const session = await useAuth().api.getSession({ headers: event.headers })
  if (!session) throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
  const body = await readBodyAs(event, Body)
  const { stored: cur } = readSsoStored()

  const next: SsoStored = {
    ...body,
    label: body.label || 'Authentik',
    clientSecret: body.clientSecret || cur.clientSecret,
  }
  const allowed = parseAllowedSubs(next.allowedSubs)

  if (next.enabled) {
    if (!isHttpUrl(discoveryFrom(next.authentikUrl, next.appSlug))) {
      reject('sso_bad_url', 'Укажите адрес Authentik: https://… или discovery URL')
    }
    if (!next.clientId) reject('sso_no_client_id', 'Укажите client_id провайдера')
    if (!next.clientSecret.trim()) reject('sso_no_client_secret', 'Укажите client_secret провайдера')
    // Куки state/PKCE защищены флагом Secure: по HTTP возврат из Authentik не сойдётся.
    if (!/^https:\/\/[^/\s]+/.test(next.redirectUrl)) {
      reject('sso_bad_redirect', 'Адрес возврата нужен https://<домен панели>/auth/authentik: вход через Authentik работает только по HTTPS')
    }
    if (allowed.size === 0) {
      reject('sso_no_allowed_subs', 'Список «кого пускать» пуст — через Authentik не войдёт никто')
    }
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

  if (!next.passwordLogin) {
    // Выключить пароль можно, только войдя через Authentik этой же сессией и
    // не меняя в том же сохранении настройки входа: иначе неверный SSO оставил
    // бы панель без входа. Break-glass /login?direct=1 тогда тоже закрыт.
    if (!next.enabled) {
      reject('sso_password_needed', 'Без SSO парольный вход выключить нельзя')
    }
    const loginMethod = (session.session as { loginMethod?: string | null }).loginMethod
    if (loginMethod !== 'sso' || !cur.enabled) {
      reject('sso_not_proven', 'Пароль выключается только после входа через Authentik: сохраните SSO, выйдите и войдите через него')
    }
    if (PROTOCOL.some(k => next[k] !== cur[k])) {
      reject('sso_not_proven', 'Пароль выключается отдельным сохранением, без правки остальных полей SSO')
    }
    const [link] = await useDb()
      .select({ sub: accounts.accountId })
      .from(accounts)
      .where(and(eq(accounts.userId, Number(session.user.id)), eq(accounts.providerId, SSO_PROVIDER)))
      .limit(1)
    if (!link || !allowed.has(link.sub)) {
      reject('sso_not_proven', 'Вашей учётки Authentik нет в списке «кого пускать»')
    }
    const probe = await probeDiscovery(discoveryFrom(next.authentikUrl, next.appSlug))
    if (!probe.ok) reject('sso_discovery_failed', probe.message)
  }

  writeSsoStored(next)
  // Список проверяется только при входе: действующие сессии убранных из него
  // (или все SSO-сессии, если SSO выключили) удаляются здесь же.
  const revoked = await revokeDisallowedSsoSessions()
  const changed = (Object.keys(next) as (keyof SsoStored)[]).filter(k => next[k] !== cur[k])
  if (changed.length) {
    const actor = actorOf({ username: String((session.user as { username?: string }).username ?? '') })
    await recordEvent({
      type: 'sso_changed', level: 'alarm', object: 'settings:sso', actor,
      title: 'Изменены настройки SSO', detail: joinDetail(next.enabled ? 'вход через Authentik включён' : 'вход через Authentik выключен', actor),
    })
  }
  // Провайдер better-auth собирается из настроек при создании экземпляра.
  resetAuth()
  useLogger().info(
    { enabled: next.enabled, clientId: next.clientId, discovery: discoveryFrom(next.authentikUrl, next.appSlug), passwordLogin: next.passwordLogin, revokedSessions: revoked },
    'sso settings updated from profile',
  )
  return { ok: true }
})
