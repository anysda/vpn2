import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { users } from '../database/schema'

/**
 * SSO через Authentik (OIDC).
 *
 * Модель доступа здесь ЖЁСТЧЕ, чем «пустил всех, кто вошёл в IdP»:
 *
 *  1. Панель НИКОГДА не заводит учётку из IdP. Только привязывает вход к уже
 *     существующей. Иначе первый же чужой вход создал бы админа — в Authentik
 *     этого флота каждый пользователь суперюзер.
 *  2. Привязка — по `sub` (uuid пользователя в IdP), не по имени и не по почте.
 *     Имя человек меняет сам, почта в панели и в IdP расходится, а auto-link по
 *     почте — вектор захвата (завёл в IdP юзера с чужой почтой → вошёл под чужим).
 *  3. Кто вообще может войти — задаётся явным списком uuid (`sso.allowedSubs`).
 *     Пустой список = не пустит никого; отсутствие привязки — не повод её создать.
 */

export interface SsoSettings {
  enabled: boolean
  autoRedirect: boolean
  passwordLogin: boolean
  /** sub → локальный логин (null = «единственная учётка панели») */
  allowed: Map<string, string | null>
}

export function ssoSettings(): SsoSettings {
  const cfg = useRuntimeConfig()
  const allowed = new Map<string, string | null>()
  for (const raw of String(cfg.sso?.allowedSubs ?? '').split(',')) {
    const item = raw.trim()
    if (!item) continue
    const [sub, username] = item.split(':')
    if (!sub?.trim()) continue
    allowed.set(sub.trim(), username?.trim() || null)
  }
  return {
    enabled: !!cfg.public?.sso?.enabled,
    autoRedirect: !!cfg.public?.sso?.autoRedirect,
    passwordLogin: cfg.sso?.passwordLogin !== false,
    allowed,
  }
}

/**
 * Настроен ли сам OIDC-клиент. Проверяется ДО редиректа в IdP: иначе человек
 * уходит на Authentik и возвращается в 500, вместо того чтобы увидеть форму.
 */
export function ssoConfigured(): boolean {
  const oidc = useRuntimeConfig().oauth?.oidc
  return !!oidc?.clientId && !!oidc?.openidConfig
}

export class SsoDenied extends Error {
  constructor(public code: string, message: string) {
    super(message)
  }
}

/**
 * Находит локальную учётку для вошедшего в IdP человека — и, если её ещё не
 * привязывали, привязывает (только когда `sub` есть в allow-списке).
 * Бросает SsoDenied, если пускать нельзя. Пользователей не создаёт.
 */
export async function resolveSsoUser(sub: string, preferredUsername?: string) {
  if (!sub) {
    throw new SsoDenied('sso_no_sub', 'IdP не вернул sub')
  }
  const db = useDb()

  const [linked] = await db.select().from(users).where(eq(users.oidcSub, sub)).limit(1)
  if (linked) return linked

  const { allowed } = ssoSettings()
  if (!allowed.has(sub)) {
    throw new SsoDenied(
      'sso_not_allowed',
      `sub ${sub} (${preferredUsername ?? '?'}) не в списке sso.allowed_subs`,
    )
  }

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

  if (target.oidcSub && target.oidcSub !== sub) {
    throw new SsoDenied(
      'sso_already_linked',
      `учётка «${target.username}» уже привязана к другому sub`,
    )
  }

  await db
    .update(users)
    .set({ oidcSub: sub, updatedAt: new Date() })
    .where(eq(users.id, target.id))

  return { ...target, oidcSub: sub }
}
