import { hash, verify } from '@node-rs/argon2'
import { base32 } from '@better-auth/utils/base32'
import { createOTP } from '@better-auth/utils/otp'
import { betterAuth, type BetterAuthPlugin } from 'better-auth'
import { drizzleAdapter } from 'better-auth/adapters/drizzle'
import { APIError, createAuthMiddleware, getSessionFromCtx } from 'better-auth/api'
import { symmetricDecrypt } from 'better-auth/crypto'
import { genericOAuth, twoFactor, username } from 'better-auth/plugins'
import { and, eq, inArray, isNull, lt, ne, notInArray, or } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { load as parseYaml } from 'js-yaml'
import { existsSync, readFileSync } from 'node:fs'
import { z } from 'zod'
import { useDb } from '../database/client'
import { accounts, sessions, twoFactors, users, verifications } from '../database/schema'

// Вход, сессии, 2FA и SSO на better-auth. Своё: argon2 с параметрами установки, 2FA-секреты из
// users.totp_secret и гашение кода, правило допуска SSO, отзыв сессий при смене пароля, 2FA и списка SSO.

const ConfigSchema = z.object({
  admin: z.object({ user: z.string().default('admin'), password: z.string().optional() }).default({ user: 'admin' }),
})

let anysdaConfig: z.infer<typeof ConfigSchema> | null = null

/** Блок `admin:` config.yaml; остальное панель берёт из NUXT_* env. */
export function loadAnysdaConfig() {
  if (anysdaConfig) return anysdaConfig
  const path = useRuntimeConfig().anysdaConfigPath
  if (!existsSync(path)) {
    useLogger().warn({ path }, 'anysda config not found; using defaults')
    return null
  }
  // Неразборный файл - громкий отказ, а не admin со случайным паролем. Исходное исключение js-yaml
  // несёт строки файла с паролем: в журнал не попадает.
  try {
    anysdaConfig = ConfigSchema.parse(parseYaml(readFileSync(path, 'utf-8')))
    return anysdaConfig
  }
  catch {
    throw new Error(`anysda config ${path} is invalid: check YAML syntax and admin field types`)
  }
}

/**
 * SSO через Authentik: учётку из IdP панель не заводит, только привязывает вход к существующей
 * по `sub` из явного списка `allowedSubs` («<sub>» или «<sub>:<логин>» через запятую).
 */
export const SSO_PROVIDER = 'authentik'

// Настройки из профиля панели; блок `sso:` config.yaml только засевает их, пока файла нет.
const SSO_PATH = '/var/lib/anysda-vpn2/runtime/sso-runtime.json'

export const SsoSchema = z.object({
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
export type SsoStored = z.infer<typeof SsoSchema>

export function parseAllowedSubs(raw: string): Map<string, string | null> {
  const items = raw.split(',').map(item => item.split(':').map(part => part.trim()))
  return new Map(items.filter(([sub]) => sub).map(([sub, login]) => [sub!, login || null]))
}

const WELL_KNOWN = '/.well-known/openid-configuration'
const AUTHENTIK_DISCOVERY = /^(.+?)\/application\/o\/([^/]+)\/\.well-known\/openid-configuration$/

export function discoveryFrom(authentikUrl: string, appSlug: string): string {
  const url = authentikUrl.trim().replace(/\/+$/, '')
  if (!url) return ''
  if (url.includes(WELL_KNOWN)) return url
  return `${url}/application/o/${appSlug.trim() || 'vpn2'}${WELL_KNOWN}`
}

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

/** Настройки как в профиле; `fromPanel` - сохранены ли они уже из панели. Битый файл - засев. */
export function readSsoStored() {
  if (!cached) {
    const seed = seedFromConfig()
    const parsed = readJsonFile<Partial<SsoStored>>(SSO_PATH)
    const stored = { ...seed }
    for (const key of Object.keys(seed) as (keyof SsoStored)[]) {
      if (parsed && typeof parsed[key] === typeof seed[key]) (stored as Record<string, unknown>)[key] = parsed[key]
    }
    cached = { stored, fromPanel: !!parsed }
  }
  return { stored: { ...cached.stored }, fromPanel: cached.fromPanel }
}

export function writeSsoStored(data: SsoStored) {
  writeJsonFile(SSO_PATH, data)
  cached = { stored: { ...data }, fromPanel: true }
}

export function maskSecret(secret: string): string {
  if (!secret) return ''
  if (secret.length < 12) return '••••••'
  return `${secret.slice(0, 4)}••••••${secret.slice(-2)}`
}

/** Адрес возврата по адресу, с которого открыт профиль: его вписывают в провайдер Authentik. */
export function defaultRedirectUrl(event: H3Event): string {
  const proto = useRuntimeConfig().session.cookie.secure ? 'https' : getRequestProtocol(event, { xForwardedProto: true })
  return `${proto}://${getRequestHost(event, { xForwardedHost: true })}/auth/authentik`
}

/** `allowed`: sub → локальный логин (null - единственная учётка панели). */
export function ssoSettings() {
  const s = readSsoStored().stored
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

class SsoDenied extends Error {
  constructor(public code: string, message: string) {
    super(message)
  }
}

/** Локальная учётка для вошедшего в IdP или SsoDenied; привязку потом создаёт better-auth по почте. */
async function resolveSsoUser(sub: string, preferredUsername?: string) {
  if (!sub) throw new SsoDenied('sso_no_sub', 'IdP не вернул sub')
  const db = useDb()

  // Список проверяется первым и для уже привязанных: привязка переживает удаление sub.
  const { allowed } = ssoSettings()
  if (!allowed.has(sub)) {
    throw new SsoDenied('sso_not_allowed', `sub ${sub} (${preferredUsername ?? '?'}) не в списке sso.allowed_subs`)
  }

  const [linked] = await db.select({ user: users }).from(accounts).innerJoin(users, eq(users.id, accounts.userId))
    .where(and(eq(accounts.providerId, SSO_PROVIDER), eq(accounts.accountId, sub))).limit(1)
  if (linked) return linked.user

  const login = allowed.get(sub)
  const candidates = await db.select().from(users).where(login ? eq(users.username, login) : undefined).limit(2)
  if (login && !candidates.length) throw new SsoDenied('sso_no_local_user', `в панели нет учётки «${login}»`)
  if (!login && candidates.length !== 1) {
    throw new SsoDenied('sso_ambiguous', `учёток в панели ${candidates.length} — в sso.allowed_subs нужен формат <sub>:<логин>`)
  }
  const target = candidates[0]!

  const [other] = await db.select({ id: accounts.id }).from(accounts)
    .where(and(eq(accounts.userId, target.id), eq(accounts.providerId, SSO_PROVIDER))).limit(1)
  if (other) throw new SsoDenied('sso_already_linked', `учётка «${target.username}» уже привязана к другому sub`)

  return target
}

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

const TOTP_PERIOD = 30
const TOTP_ISSUER = 'anysda-vpn2'

type OtpKey = string | CryptoKey
// В типах createOTP ключ только строка, но CryptoKey проходит как есть.
const otp = (key: OtpKey) => createOTP(key as string, { period: TOTP_PERIOD, digits: 6 })

/** Секрет из users.totp_secret: base32 от сырых байт ключа. */
async function legacyKey(secret: string): Promise<CryptoKey> {
  const bytes = base32.decode(secret.toUpperCase().replace(/[\s=]/g, ''))
  return crypto.subtle.importKey('raw', bytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign'])
}

/** check - чем проверять код человека, library - секрет библиотеки. */
async function totpKeys(secretConfig: unknown, userId: number): Promise<{ check: OtpKey, library: string } | null> {
  const [row] = await useDb().select({ legacy: users.legacyTotpSecret, secret: twoFactors.secret })
    .from(twoFactors).innerJoin(users, eq(users.id, twoFactors.userId)).where(eq(twoFactors.userId, userId)).limit(1)
  if (!row) return null
  const library = await symmetricDecrypt({ key: secretConfig as string, data: row.secret })
  return { check: row.legacy ? await legacyKey(row.legacy) : library, library }
}

const codeOf = (code: unknown) => String(code ?? '').replace(/\s/g, '')
const isCode = (code: unknown) => /^\d{6}$/.test(codeOf(code))

/** Код ±1 шаг гасится условным update: из параллельных запросов пройдёт один. */
async function consumeCode(userId: number, key: OtpKey, code: unknown): Promise<number | null> {
  const token = codeOf(code)
  if (!isCode(token)) return null
  const now = Math.floor(Date.now() / 1000 / TOTP_PERIOD)
  let step: number | null = null
  for (const s of [now - 1, now, now + 1]) {
    if (await otp(key).hotp(s) === token) step = s
  }
  if (step === null) return null
  const won = await useDb().update(users).set({ totpLastStep: step })
    .where(and(eq(users.id, userId), or(isNull(users.totpLastStep), lt(users.totpLastStep, step)))).returning({ id: users.id })
  return won.length === 1 ? step : null
}

/** Форма входа без авторедиректа в SSO: break-glass. */
export const LOGIN_FORM = '/login?direct=1'

const ARGON2 = { memoryCost: 65536, timeCost: 3, parallelism: 4 }
const TWO_FACTOR_COOKIE = 'two_factor'
type Middleware = Parameters<typeof createAuthMiddleware>[0]
type HookCtx = Parameters<Middleware>[0]

export const hashPassword = (plain: string): Promise<string> => hash(plain, ARGON2)
export const verifyPassword = (hashed: string, plain: string): Promise<boolean> => verify(hashed, plain).catch(() => false)

/** Удаляет SSO-сессии учёток, чей sub больше не в списке (при выключенном SSO - все). */
export async function revokeDisallowedSsoSessions(sso = ssoSettings()): Promise<number> {
  const subs = sso.enabled ? [...sso.allowed.keys()] : []
  const db = useDb()
  const isSso = eq(sessions.loginMethod, 'sso')
  const allowedUsers = db.select({ id: accounts.userId }).from(accounts)
    .where(and(eq(accounts.providerId, SSO_PROVIDER), inArray(accounts.accountId, subs)))
  const gone = await db.delete(sessions)
    .where(subs.length ? and(isSso, notInArray(sessions.userId, allowedUsers)) : isSso)
    .returning({ id: sessions.id })
  return gone.length
}

/** Чей код проверяется: из сессии (настройка 2FA) или из cookie второго шага входа. */
async function twoFactorUserId(ctx: HookCtx): Promise<number | null> {
  const session = await getSessionFromCtx(ctx)
  if (session) return Number(session.user.id)
  const signed = await ctx.getSignedCookie(ctx.context.createAuthCookie(TWO_FACTOR_COOKIE).name, ctx.context.secret)
  const v = signed ? await ctx.context.internalAdapter.findVerificationValue(signed) : null
  return v ? Number(v.value) : null
}

function requireCodeShape(code: unknown) {
  if (!isCode(code)) {
    throw APIError.from('BAD_REQUEST', { code: 'VALIDATION_ERROR', message: '[body.code] 6 digits expected' })
  }
}

const hook = (paths: string[], handler: Middleware) =>
  ({ matcher: (ctx: { path?: string }) => paths.includes(ctx.path ?? ''), handler: createAuthMiddleware(handler) })

const panelGuard = (): BetterAuthPlugin => ({
  id: 'panel-guard',
  hooks: {
    before: [
      // NUXT_SSO_PASSWORD_LOGIN=false закрывает парольный вход совсем, break-glass тоже.
      hook(['/sign-in/username'], async () => {
        if (!ssoSettings().passwordLogin) throw new APIError('FORBIDDEN', { code: 'PASSWORD_LOGIN_DISABLED', message: 'Password login disabled' })
      }),
      // Код проверяется здесь (секреты users.totp_secret, гашение повтора), а библиотеке уходит
      // заведомо верный или неверный: счётчики неудач ведёт она.
      hook(['/two-factor/verify-totp'], async (ctx) => {
        const body = { ...(ctx.body ?? {}), trustDevice: false }
        requireCodeShape(body.code)
        const userId = await twoFactorUserId(ctx)
        const keys = userId ? await totpKeys(ctx.context.secretConfig, userId) : null
        if (!userId || !keys) return { context: { body } }
        const step = await consumeCode(userId, keys.check, body.code)
        body.code = await otp(keys.library).hotp(step ?? Math.floor(Date.now() / 1000 / TOTP_PERIOD) + 10)
        return { context: { body } }
      }),
      // Выключение 2FA: пароль и действующий код (библиотека спрашивает только пароль).
      hook(['/two-factor/disable'], async (ctx) => {
        const session = await getSessionFromCtx(ctx)
        if (!session) return
        const credential = (await ctx.context.internalAdapter.findAccounts(session.user.id)).find(a => a.providerId === 'credential')
        const password = String(ctx.body?.password ?? '')
        if (!credential?.password || !(await ctx.context.password.verify({ hash: credential.password, password }))) {
          throw APIError.from('BAD_REQUEST', { code: 'INVALID_PASSWORD', message: 'Invalid password' })
        }
        const userId = Number(session.user.id)
        const keys = await totpKeys(ctx.context.secretConfig, userId)
        if (!keys) return
        requireCodeShape(ctx.body?.code)
        if ((await consumeCode(userId, keys.check, ctx.body?.code)) === null) {
          throw APIError.from('UNAUTHORIZED', { code: 'INVALID_CODE', message: 'Invalid code' })
        }
      }),
      hook(['/change-password'], async (ctx) => {
        if (typeof ctx.body?.newPassword === 'string' && ctx.body.newPassword.length > 256) {
          throw APIError.from('BAD_REQUEST', { code: 'PASSWORD_TOO_LONG', message: 'Password is too long' })
        }
        return { context: { body: { ...(ctx.body ?? {}), revokeOtherSessions: true } } }
      }),
    ],
    after: [
      // Метка в аутентификаторе: «anysda-vpn2:<логин>» вместо синтетической почты.
      hook(['/two-factor/enable'], async (ctx) => {
        const r = ctx.context.returned as { totpURI?: unknown } | undefined
        if (!r || typeof r !== 'object' || typeof r.totpURI !== 'string') return
        const session = await getSessionFromCtx(ctx)
        if (!session) return
        const name = String((session.user as { username?: string }).username ?? session.user.name)
        const query = r.totpURI.split('?')[1] ?? ''
        return ctx.json({ method: 'totp', totpURI: `otpauth://totp/${encodeURIComponent(`${TOTP_ISSUER}:${name}`)}?${query}` })
      }),
      // Включение и выключение 2FA пересоздают текущую сессию, остальные рвутся здесь.
      hook(['/two-factor/verify-totp', '/two-factor/disable'], async (ctx) => {
        const fresh = ctx.context.newSession
        if (!fresh) return
        if (ctx.path === '/two-factor/verify-totp' && ctx.getCookie(ctx.context.createAuthCookie(TWO_FACTOR_COOKIE).name)) return
        const userId = Number(fresh.user.id)
        await useDb().delete(sessions).where(and(eq(sessions.userId, userId), ne(sessions.token, fresh.session.token)))
        if (ctx.path === '/two-factor/disable') {
          await useDb().update(users).set({ legacyTotpSecret: null, totpLastStep: null }).where(eq(users.id, userId))
        }
      }),
    ],
  },
})

function ssoProvider(sso: ReturnType<typeof ssoSettings>) {
  return {
    providerId: SSO_PROVIDER,
    discoveryUrl: sso.discoveryUrl,
    clientId: sso.clientId,
    clientSecret: sso.clientSecret,
    scopes: ['openid', 'profile', 'email'],
    pkce: true,
    ...(sso.redirectUrl ? { redirectURI: sso.redirectUrl } : {}),
    disableImplicitSignUp: true,
    disableSignUp: true,
    mapProfileToUser: async (profile: Record<string, unknown>) => {
      const sub = String(profile.sub ?? '')
      const preferred = profile.preferred_username as string | undefined
      try {
        const user = await resolveSsoUser(sub, preferred)
        useLogger().info({ sub, username: user.username }, 'sso: вход через Authentik')
        return { email: user.email, emailVerified: true, name: user.name }
      }
      catch (err) {
        if (!(err instanceof SsoDenied)) throw err
        // Причина в журнал, в браузер только код.
        useLogger().warn({ sub, preferred, reason: err.message }, 'sso: вход отклонён')
        throw new APIError('FOUND', undefined, { Location: `${LOGIN_FORM}&error=${err.code}` })
      }
    },
  }
}

function buildAuth() {
  const cfg = useRuntimeConfig()
  const sso = ssoSettings()
  // Попытки входа, смены пароля и кода 2FA - на адрес и ручку за 5 минут.
  const attempts = { window: 300, max: Number(cfg.authLoginMaxAttempts) || 10 }
  return betterAuth({
    appName: TOTP_ISSUER,
    secret: String(cfg.session.password ?? ''),
    // Адрес панели - из Host запроса: у установки их бывает несколько.
    baseURL: { allowedHosts: ['*'], protocol: cfg.session.cookie.secure ? 'https' : 'http' },
    basePath: '/api/auth',
    database: drizzleAdapter(useDb(), { provider: 'sqlite', schema: { users, sessions, accounts, verifications, twoFactors } }),
    user: { modelName: 'users' },
    session: {
      modelName: 'sessions',
      expiresIn: 60 * 60 * 24,
      cookieCache: { enabled: false },
      // Без defaultValue: оно затёрло бы значение при пересоздании сессии.
      additionalFields: { loginMethod: { type: 'string', required: false, input: false } },
    },
    account: { modelName: 'accounts', accountLinking: { enabled: true, trustedProviders: [SSO_PROVIDER] } },
    verification: { modelName: 'verifications' },
    emailAndPassword: {
      enabled: true,
      disableSignUp: true,
      minPasswordLength: 8,
      // Пароли установки любой длины; новый ограничен в хуке /change-password.
      maxPasswordLength: Number.MAX_SAFE_INTEGER,
      password: { hash: hashPassword, verify: ({ hash, password }) => verifyPassword(hash, password) },
    },
    advanced: {
      database: { generateId: 'serial' },
      useSecureCookies: !!cfg.session.cookie.secure,
      // X-Forwarded-For выставляет ручка /api/auth/*: адрес, которому она верит.
      ipAddress: { ipAddressHeaders: ['x-forwarded-for'] },
    },
    rateLimit: {
      enabled: true,
      storage: 'memory',
      window: 60,
      max: 100,
      customRules: { '/sign-in/username': attempts, '/change-password': attempts, '/two-factor/*': attempts, '/get-session': false },
    },
    databaseHooks: {
      session: {
        create: {
          before: async (session, ctx) => {
            if ((session as { loginMethod?: string }).loginMethod) return
            return { data: { ...session, loginMethod: ctx?.path?.startsWith('/callback/') ? 'sso' : 'password' } }
          },
        },
      },
    },
    plugins: [
      username({ minUsernameLength: 1, maxUsernameLength: 64, usernameNormalization: false, usernameValidator: () => true }),
      twoFactor({ issuer: TOTP_ISSUER, schema: { twoFactor: { modelName: 'twoFactors' } } }),
      genericOAuth({ config: sso.enabled ? [ssoProvider(sso)] : [] }),
      panelGuard(),
    ],
  })
}

let auth: ReturnType<typeof buildAuth> | null = null
export const useAuth = () => (auth ??= buildAuth())
export const resetAuth = () => { auth = null }

/** Сессия запроса или 401. */
export async function requireAuth(event: H3Event) {
  const session = await useAuth().api.getSession({ headers: event.headers })
  if (!session) throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
  return { id: Number(session.user.id), username: String((session.user as { username?: string }).username ?? '') }
}
