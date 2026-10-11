import { and, eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { accounts, users } from '../database/schema'

/**
 * SSO через Authentik (OIDC). Протокол ведёт better-auth (server/utils/auth.ts),
 * здесь только настройки и правило «кого пускаем».
 *
 * Модель доступа ЖЁСТЧЕ, чем «пустил всех, кто вошёл в IdP»:
 *
 *  1. Панель НИКОГДА не заводит учётку из IdP. Только привязывает вход к уже
 *     существующей. Иначе первый же чужой вход создал бы админа — в Authentik
 *     этого флота каждый пользователь суперюзер.
 *  2. Привязка — по `sub` (uuid пользователя в IdP), не по имени и не по почте
 *     из IdP: имя человек меняет сам, а auto-link по почте — вектор захвата.
 *     Почту better-auth получает от нас (синтетическую почту выбранной учётки),
 *     не от IdP.
 *  3. Кто вообще может войти — задаётся явным списком uuid (`sso.allowedSubs`).
 *     Пустой список = не пустит никого; отсутствие привязки — не повод её создать.
 */

export const SSO_PROVIDER = 'authentik'

export interface SsoSettings {
  enabled: boolean
  autoRedirect: boolean
  passwordLogin: boolean
  label: string
  /** sub → локальный логин (null = «единственная учётка панели») */
  allowed: Map<string, string | null>
  discoveryUrl: string
  clientId: string
  clientSecret: string
  redirectUrl: string
}

function parseAllowedSubs(raw: string): Map<string, string | null> {
  const allowed = new Map<string, string | null>()
  for (const part of raw.split(',')) {
    const item = part.trim()
    if (!item) continue
    const [sub, username] = item.split(':')
    if (!sub?.trim()) continue
    allowed.set(sub.trim(), username?.trim() || null)
  }
  return allowed
}

export function ssoSettings(): SsoSettings {
  const cfg = useRuntimeConfig()
  const oidc = cfg.oauth.oidc
  return {
    enabled: !!cfg.public.sso.enabled,
    autoRedirect: !!cfg.public.sso.autoRedirect,
    passwordLogin: cfg.sso.passwordLogin !== false,
    label: String(cfg.public.sso.label || 'Authentik'),
    allowed: parseAllowedSubs(String(cfg.sso.allowedSubs ?? '')),
    discoveryUrl: String(oidc.openidConfig ?? ''),
    clientId: String(oidc.clientId ?? ''),
    clientSecret: String(oidc.clientSecret ?? ''),
    redirectUrl: String(oidc.redirectURL ?? ''),
  }
}

/**
 * Настроен ли сам OIDC-клиент. Проверяется ДО редиректа в IdP: иначе человек
 * уходит на Authentik и возвращается в ошибку, вместо того чтобы увидеть форму.
 */
export function ssoConfigured(s: SsoSettings = ssoSettings()): boolean {
  return !!s.clientId && !!s.discoveryUrl
}

export class SsoDenied extends Error {
  constructor(public code: string, message: string) {
    super(message)
  }
}

/**
 * Находит локальную учётку для вошедшего в IdP человека. Бросает SsoDenied,
 * если пускать нельзя. Ничего не пишет: привязку (строку accounts) после этого
 * создаёт better-auth, найдя учётку по возвращённой почте.
 */
export async function resolveSsoUser(sub: string, preferredUsername?: string) {
  if (!sub) {
    throw new SsoDenied('sso_no_sub', 'IdP не вернул sub')
  }
  const db = useDb()

  // Список проверяется ПЕРВЫМ, и для уже привязанных тоже: привязка в БД
  // переживает удаление sub из sso.allowed_subs, и без этой проверки убранный
  // из списка человек продолжал бы входить (VPN2-43).
  const { allowed } = ssoSettings()
  if (!allowed.has(sub)) {
    throw new SsoDenied(
      'sso_not_allowed',
      `sub ${sub} (${preferredUsername ?? '?'}) не в списке sso.allowed_subs`,
    )
  }

  const [linked] = await db
    .select({ user: users })
    .from(accounts)
    .innerJoin(users, eq(users.id, accounts.userId))
    .where(and(eq(accounts.providerId, SSO_PROVIDER), eq(accounts.accountId, sub)))
    .limit(1)
  if (linked) return linked.user

  const wantUsername = allowed.get(sub)
  let target: typeof users.$inferSelect | undefined

  if (wantUsername) {
    const found = await db.select().from(users).where(eq(users.username, wantUsername)).limit(1)
    target = found[0]
    if (!target) {
      throw new SsoDenied('sso_no_local_user', `в панели нет учётки «${wantUsername}»`)
    }
  }
  else {
    // Логин не указан — привязываем к единственной учётке панели. Если их
    // несколько, угадывать не будем: учётку надо назвать явно.
    const all = await db.select().from(users).limit(2)
    if (all.length !== 1) {
      throw new SsoDenied(
        'sso_ambiguous',
        `учёток в панели ${all.length} — в sso.allowed_subs нужен формат <sub>:<логин>`,
      )
    }
    target = all[0]!
  }

  const [other] = await db
    .select({ id: accounts.id })
    .from(accounts)
    .where(and(eq(accounts.userId, target.id), eq(accounts.providerId, SSO_PROVIDER)))
    .limit(1)
  if (other) {
    throw new SsoDenied(
      'sso_already_linked',
      `учётка «${target.username}» уже привязана к другому sub`,
    )
  }

  return target
}
