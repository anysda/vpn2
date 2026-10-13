import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { H3Event } from 'h3'
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
 *  3. Кто вообще может войти — задаётся явным списком uuid (`allowedSubs`).
 *     Пустой список = не пустит никого; отсутствие привязки — не повод её создать.
 */

export const SSO_PROVIDER = 'authentik'

/**
 * Настройки SSO задаются в профиле панели и лежат здесь. Блок `sso:` в
 * config.yaml (через env стадии 30) только засевает их, пока файла нет: после
 * первого сохранения из профиля главнее панель, повторный деплой файл не трогает.
 */
const RUNTIME_PATH = '/etc/anysda/sso-runtime.json'

/** То, что хранится и правится в профиле. */
export interface SsoStored {
  enabled: boolean
  /** адрес Authentik (https://auth.example.com) или сразу discovery URL */
  authentikUrl: string
  /** slug приложения в Authentik; не нужен, если authentikUrl — discovery URL */
  appSlug: string
  clientId: string
  clientSecret: string
  /** «<sub>» или «<sub>:<логин>» через запятую */
  allowedSubs: string
  label: string
  autoRedirect: boolean
  passwordLogin: boolean
  /** адрес возврата, его же надо прописать у провайдера в Authentik */
  redirectUrl: string
}

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

export function parseAllowedSubs(raw: string): Map<string, string | null> {
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

const WELL_KNOWN = '/.well-known/openid-configuration'
const AUTHENTIK_DISCOVERY = /^(.+?)\/application\/o\/([^/]+)\/\.well-known\/openid-configuration$/

/** Discovery URL из адреса Authentik и slug — как в config2env.py. */
export function discoveryFrom(authentikUrl: string, appSlug: string): string {
  const url = authentikUrl.trim().replace(/\/+$/, '')
  if (!url) return ''
  if (url.includes(WELL_KNOWN)) return url
  return `${url}/application/o/${appSlug.trim() || 'vpn2'}${WELL_KNOWN}`
}

/** Засев из env стадии 30: так было до настроек в профиле. */
function seedFromConfig(): SsoStored {
  const cfg = useRuntimeConfig()
  const oidc = cfg.oauth.oidc
  const discovery = String(oidc.openidConfig ?? '')
  const m = AUTHENTIK_DISCOVERY.exec(discovery)
  return {
    enabled: !!cfg.public.sso.enabled,
    authentikUrl: m ? m[1]! : discovery,
    appSlug: m ? m[2]! : '',
    clientId: String(oidc.clientId ?? ''),
    clientSecret: String(oidc.clientSecret ?? ''),
    allowedSubs: String(cfg.sso.allowedSubs ?? ''),
    label: String(cfg.public.sso.label || 'Authentik'),
    autoRedirect: !!cfg.public.sso.autoRedirect,
    passwordLogin: cfg.sso.passwordLogin !== false,
    redirectUrl: String(oidc.redirectURL ?? ''),
  }
}

let cached: { stored: SsoStored, fromPanel: boolean } | null = null

function load(): { stored: SsoStored, fromPanel: boolean } {
  if (cached) return cached
  const seed = seedFromConfig()
  try {
    if (existsSync(RUNTIME_PATH)) {
      const parsed = JSON.parse(readFileSync(RUNTIME_PATH, 'utf-8')) as Partial<SsoStored>
      const stored = { ...seed }
      for (const key of Object.keys(seed) as (keyof SsoStored)[]) {
        if (typeof parsed[key] === typeof seed[key]) (stored as Record<string, unknown>)[key] = parsed[key]
      }
      cached = { stored, fromPanel: true }
      return cached
    }
  }
  catch (err) {
    // Битый файл — не повод запереть панель: живём на засеве из config.yaml.
    useLogger().warn({ err }, 'failed to read sso-runtime.json')
  }
  cached = { stored: seed, fromPanel: false }
  return cached
}

/** Настройки как в профиле; `fromPanel` — сохранены ли они уже из панели. */
export function readSsoStored() {
  const { stored, fromPanel } = load()
  return { stored: { ...stored }, fromPanel }
}

export function writeSsoStored(data: SsoStored) {
  const dir = dirname(RUNTIME_PATH)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  writeFileSync(RUNTIME_PATH, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 })
  cached = { stored: { ...data }, fromPanel: true }
}

export function maskSecret(secret: string): string {
  if (!secret) return ''
  if (secret.length < 12) return '••••••'
  return `${secret.slice(0, 4)}••••••${secret.slice(-2)}`
}

/** Адрес возврата по адресу, с которого открыт профиль: его вписывают в провайдер. */
export function defaultRedirectUrl(event: H3Event): string {
  const proto = useRuntimeConfig().session.cookie.secure ? 'https' : getRequestProtocol(event, { xForwardedProto: true })
  return `${proto}://${getRequestHost(event, { xForwardedHost: true })}/auth/authentik`
}

export function ssoSettings(): SsoSettings {
  const s = load().stored
  const discoveryUrl = discoveryFrom(s.authentikUrl, s.appSlug)
  const enabled = s.enabled && !!s.clientId.trim() && !!discoveryUrl && !!s.clientSecret.trim()
  return {
    enabled,
    autoRedirect: enabled && s.autoRedirect,
    passwordLogin: !enabled || s.passwordLogin,
    label: s.label || 'Authentik',
    allowed: parseAllowedSubs(s.allowedSubs),
    discoveryUrl,
    clientId: s.clientId,
    clientSecret: s.clientSecret,
    redirectUrl: s.redirectUrl,
  }
}

/**
 * Настроен ли сам OIDC-клиент. Проверяется ДО редиректа в IdP: иначе человек
 * уходит на Authentik и возвращается в ошибку, вместо того чтобы увидеть форму.
 */
export function ssoConfigured(s: SsoSettings = ssoSettings()): boolean {
  return !!s.clientId.trim() && !!s.discoveryUrl && !!s.clientSecret.trim()
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

/**
 * Читает discovery: отвечает ли там OpenID-провайдер. Для кнопки «Проверить»
 * в профиле и перед выключением парольного входа.
 */
export async function probeDiscovery(url: string): Promise<{ ok: true, issuer: string } | { ok: false, message: string }> {
  let res: Response
  try {
    res = await fetch(url, { signal: AbortSignal.timeout(5000), headers: { accept: 'application/json' } })
  }
  catch (err) {
    const reason = err instanceof Error && err.name === 'TimeoutError' ? 'нет ответа за 5 секунд' : 'адрес недоступен'
    return { ok: false, message: `Discovery не прочитан: ${reason}` }
  }
  if (!res.ok) return { ok: false, message: `Discovery не прочитан: HTTP ${res.status}` }
  const doc = await res.json().catch(() => null) as { issuer?: unknown, authorization_endpoint?: unknown } | null
  if (!doc || typeof doc.issuer !== 'string' || typeof doc.authorization_endpoint !== 'string') {
    return { ok: false, message: 'По адресу не discovery OpenID: нет issuer или authorization_endpoint' }
  }
  return { ok: true, issuer: doc.issuer }
}
