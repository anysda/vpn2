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
import { useDb } from '../database/client'
import * as schema from '../database/schema'
import { resolveSsoUser, SSO_PROVIDER, SsoDenied, ssoConfigured, ssoSettings } from './sso'

/**
 * Вход, сессии, 2FA и SSO панели — на better-auth. Своего здесь только то,
 * чего в библиотеке нет:
 *  - argon2 с прежними параметрами, чтобы старые хеши паролей подходили;
 *  - проверка 2FA-секретов, заведённых до библиотеки, и гашение кода
 *    (код этого и более раннего шага второй раз не пройдёт, VPN2-62);
 *  - правило «кого пускаем через SSO» (utils/sso.ts);
 *  - отзыв остальных сессий при смене пароля и включении/выключении 2FA;
 *  - отзыв SSO-сессий тех, кого убрали из списка SSO.
 */

/** Форма входа, где нет авторедиректа в SSO: break-glass. */
export const LOGIN_FORM = '/login?direct=1'

const ARGON2 = { memoryCost: 65536, timeCost: 3, parallelism: 4 }

export function hashPassword(plain: string): Promise<string> {
  return hash(plain, ARGON2)
}

export async function verifyPassword(hashed: string, plain: string): Promise<boolean> {
  try {
    return await verify(hashed, plain)
  }
  catch {
    return false
  }
}

// --- 2FA -------------------------------------------------------------------

const TOTP_PERIOD = 30
const TOTP_ISSUER = 'anysda-vpn2'

type OtpKey = string | CryptoKey
// createOTP в типах принимает только строку, но внутри отдаёт ключ в
// WebCrypto как есть — CryptoKey проходит (сверено с RFC 4226).
const otp = (key: OtpKey) => createOTP(key as string, { period: TOTP_PERIOD, digits: 6 })

/** Секрет, заведённый до better-auth: base32 от сырых байт ключа. */
async function legacyKey(secret: string): Promise<CryptoKey> {
  const bytes = base32.decode(secret.toUpperCase().replace(/[\s=]/g, ''))
  return crypto.subtle.importKey('raw', bytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign'])
}

interface TotpKeys {
  /** чем проверять код человека */
  check: OtpKey
  /** секрет библиотеки — им проверяет она сама */
  library: string
}

async function totpKeys(secretConfig: unknown, userId: number): Promise<TotpKeys | null> {
  const db = useDb()
  const [row] = await db
    .select({ legacy: schema.users.legacyTotpSecret, secret: schema.twoFactors.secret })
    .from(schema.twoFactors)
    .innerJoin(schema.users, eq(schema.users.id, schema.twoFactors.userId))
    .where(eq(schema.twoFactors.userId, userId))
    .limit(1)
  if (!row) return null
  const library = await symmetricDecrypt({ key: secretConfig as string, data: row.secret })
  return { check: row.legacy ? await legacyKey(row.legacy) : library, library }
}

/**
 * Проверяет код (±1 шаг на расхождение часов) и гасит его: шаг пишется в
 * users.totp_last_step условным update — из двух параллельных запросов с
 * одним кодом пройдёт ровно один. Возвращает шаг или null.
 */
async function consumeCode(userId: number, key: OtpKey, code: unknown): Promise<number | null> {
  const token = String(code ?? '').replace(/\s/g, '')
  if (!/^\d{6}$/.test(token)) return null
  const now = Math.floor(Date.now() / 1000 / TOTP_PERIOD)
  let step: number | null = null
  for (const s of [now - 1, now, now + 1]) {
    if (await otp(key).hotp(s) === token) step = s
  }
  if (step === null) return null
  const won = await useDb()
    .update(schema.users)
    .set({ totpLastStep: step })
    .where(and(
      eq(schema.users.id, userId),
      or(isNull(schema.users.totpLastStep), lt(schema.users.totpLastStep, step)),
    ))
    .returning({ id: schema.users.id })
  return won.length === 1 ? step : null
}

/** Удаляет все сессии учётки, кроме `keepToken`. */
async function revokeOtherSessions(userId: number, keepToken: string) {
  await useDb()
    .delete(schema.sessions)
    .where(and(eq(schema.sessions.userId, userId), ne(schema.sessions.token, keepToken)))
}

/**
 * Удаляет SSO-сессии учёток, чей sub больше не в списке SSO; при выключенном
 * SSO — все SSO-сессии. Список проверяется при входе (resolveSsoUser), а
 * сессия живёт сутки: без этого убранный продолжал бы работать до её конца.
 * Вход паролем список SSO не касается. Возвращает число удалённых сессий.
 */
export async function revokeDisallowedSsoSessions(sso = ssoSettings()): Promise<number> {
  const subs = sso.enabled ? [...sso.allowed.keys()] : []
  const db = useDb()
  const isSso = eq(schema.sessions.loginMethod, 'sso')
  const allowedUsers = db.select({ id: schema.accounts.userId }).from(schema.accounts)
    .where(and(eq(schema.accounts.providerId, SSO_PROVIDER), inArray(schema.accounts.accountId, subs)))
  const gone = await db.delete(schema.sessions)
    .where(subs.length ? and(isSso, notInArray(schema.sessions.userId, allowedUsers)) : isSso)
    .returning({ id: schema.sessions.id })
  return gone.length
}

const TWO_FACTOR_COOKIE = 'two_factor'

type HookCtx = Parameters<Parameters<typeof createAuthMiddleware>[0]>[0]

/** Чей код проверяется: из сессии (настройка 2FA) или из cookie второго шага входа. */
async function twoFactorUserId(ctx: HookCtx): Promise<number | null> {
  const session = await getSessionFromCtx(ctx)
  if (session) return Number(session.user.id)
  const cookie = ctx.context.createAuthCookie(TWO_FACTOR_COOKIE)
  const signed = await ctx.getSignedCookie(cookie.name, ctx.context.secret)
  if (!signed) return null
  const v = await ctx.context.internalAdapter.findVerificationValue(signed)
  return v ? Number(v.value) : null
}

/** Код не из 6 цифр — ошибка формы, а не неверный код: человек видит, что поправить. */
function requireCodeShape(code: unknown) {
  if (!/^\d{6}$/.test(String(code ?? '').replace(/\s/g, ''))) {
    throw APIError.from('BAD_REQUEST', { code: 'VALIDATION_ERROR', message: '[body.code] 6 digits expected' })
  }
}

function isSignInStep(ctx: HookCtx): boolean {
  return !!ctx.getCookie(ctx.context.createAuthCookie(TWO_FACTOR_COOKIE).name)
}

const panelGuard = (): BetterAuthPlugin => ({
  id: 'panel-guard',
  hooks: {
    before: [
      {
        // Парольный вход по стандарту флота не удаляется, а прячется с формы:
        // при включённом SSO сюда попадают через break-glass /login?direct=1.
        // NUXT_SSO_PASSWORD_LOGIN=false рубит его наглухо.
        matcher: ctx => ctx.path === '/sign-in/username',
        handler: createAuthMiddleware(async () => {
          if (!ssoSettings().passwordLogin) {
            throw new APIError('FORBIDDEN', { code: 'PASSWORD_LOGIN_DISABLED', message: 'Password login disabled' })
          }
        }),
      },
      {
        // Код проверяем сами: библиотека не умеет старые секреты и не гасит
        // повтор кода. Дальше она получает код, заведомо подходящий (проверка
        // прошла) или заведомо нет (не прошла) — счётчики неудач и блокировку
        // ведёт она.
        matcher: ctx => ctx.path === '/two-factor/verify-totp',
        handler: createAuthMiddleware(async (ctx) => {
          const body = { ...(ctx.body ?? {}), trustDevice: false }
          requireCodeShape(body.code)
          const userId = await twoFactorUserId(ctx)
          const keys = userId ? await totpKeys(ctx.context.secretConfig, userId) : null
          if (!userId || !keys) return { context: { body } }
          const now = Math.floor(Date.now() / 1000 / TOTP_PERIOD)
          const step = await consumeCode(userId, keys.check, body.code)
          body.code = await otp(keys.library).hotp(step ?? now + 10)
          return { context: { body } }
        }),
      },
      {
        // Выключение 2FA: пароль и действующий код. Библиотека спрашивает
        // только пароль.
        matcher: ctx => ctx.path === '/two-factor/disable',
        handler: createAuthMiddleware(async (ctx) => {
          const session = await getSessionFromCtx(ctx)
          if (!session) return
          const userId = Number(session.user.id)
          const credential = (await ctx.context.internalAdapter.findAccounts(session.user.id))
            .find(a => a.providerId === 'credential')
          const password = String(ctx.body?.password ?? '')
          if (!credential?.password || !(await ctx.context.password.verify({ hash: credential.password, password }))) {
            throw APIError.from('BAD_REQUEST', { code: 'INVALID_PASSWORD', message: 'Invalid password' })
          }
          const keys = await totpKeys(ctx.context.secretConfig, userId)
          if (keys) requireCodeShape(ctx.body?.code)
          if (keys && (await consumeCode(userId, keys.check, ctx.body?.code)) === null) {
            throw APIError.from('UNAUTHORIZED', { code: 'INVALID_CODE', message: 'Invalid code' })
          }
        }),
      },
      {
        // Смена пароля всегда рвёт остальные сессии.
        matcher: ctx => ctx.path === '/change-password',
        handler: createAuthMiddleware(async (ctx) => {
          if (typeof ctx.body?.newPassword === 'string' && ctx.body.newPassword.length > 256) {
            throw APIError.from('BAD_REQUEST', { code: 'PASSWORD_TOO_LONG', message: 'Password is too long' })
          }
          return { context: { body: { ...(ctx.body ?? {}), revokeOtherSessions: true } } }
        }),
      },
    ],
    after: [
      {
        // Метку в приложении-аутентификаторе библиотека строит из почты, а она
        // у нас синтетическая — ставим прежнюю «anysda-vpn2:<логин>». Резервные
        // коды не отдаём: их ручки отключены.
        matcher: ctx => ctx.path === '/two-factor/enable',
        handler: createAuthMiddleware(async (ctx) => {
          const r = ctx.context.returned as { totpURI?: unknown } | undefined
          if (!r || typeof r !== 'object' || typeof r.totpURI !== 'string') return
          const session = await getSessionFromCtx(ctx)
          if (!session) return
          const name = String((session.user as { username?: string }).username ?? session.user.name)
          const query = r.totpURI.split('?')[1] ?? ''
          return ctx.json({
            method: 'totp',
            totpURI: `otpauth://totp/${encodeURIComponent(`${TOTP_ISSUER}:${name}`)}?${query}`,
          })
        }),
      },
      {
        // Включение и выключение 2FA пересоздают только текущую сессию —
        // остальные рвём сами.
        matcher: ctx => ctx.path === '/two-factor/verify-totp' || ctx.path === '/two-factor/disable',
        handler: createAuthMiddleware(async (ctx) => {
          const fresh = ctx.context.newSession
          if (!fresh) return
          if (ctx.path === '/two-factor/verify-totp' && isSignInStep(ctx)) return
          const userId = Number(fresh.user.id)
          await revokeOtherSessions(userId, fresh.session.token)
          if (ctx.path === '/two-factor/disable') {
            await useDb()
              .update(schema.users)
              .set({ legacyTotpSecret: null, totpLastStep: null })
              .where(eq(schema.users.id, userId))
          }
        }),
      },
    ],
  },
})

// --- экземпляр ---------------------------------------------------------------

function buildAuth() {
  const cfg = useRuntimeConfig()
  const sso = ssoSettings()

  return betterAuth({
    appName: TOTP_ISSUER,
    secret: String(cfg.session.password ?? ''),
    // Адрес панели берётся из Host запроса: у одной установки их бывает
    // несколько (домен, IP), а перечислять их здесь нечем.
    baseURL: {
      allowedHosts: ['*'],
      protocol: cfg.session.cookie.secure ? 'https' : 'http',
    },
    basePath: '/api/auth',
    database: drizzleAdapter(useDb(), {
      provider: 'sqlite',
      schema: {
        users: schema.users,
        sessions: schema.sessions,
        accounts: schema.accounts,
        verifications: schema.verifications,
        twoFactors: schema.twoFactors,
      },
    }),
    user: { modelName: 'users' },
    session: {
      modelName: 'sessions',
      expiresIn: 60 * 60 * 24,
      cookieCache: { enabled: false },
      additionalFields: {
        // Без defaultValue: при вкл/выкл 2FA библиотека пересоздаёт сессию
        // из старой, и значение по умолчанию затёрло бы перенесённое.
        loginMethod: { type: 'string', required: false, input: false },
      },
    },
    account: {
      modelName: 'accounts',
      // Почту для привязки выдаёт resolveSsoUser, а не IdP.
      accountLinking: { enabled: true, trustedProviders: [SSO_PROVIDER] },
    },
    verification: { modelName: 'verifications' },
    emailAndPassword: {
      enabled: true,
      disableSignUp: true,
      minPasswordLength: 8,
      // В config.yaml и старом входе длина пароля не ограничивалась. Лимит
      // библиотеки применяется и при проверке существующего пароля, поэтому
      // ограничиваем только новый пароль в хуке /change-password выше.
      maxPasswordLength: Number.MAX_SAFE_INTEGER,
      password: {
        hash: hashPassword,
        verify: ({ hash, password }) => verifyPassword(hash, password),
      },
    },
    advanced: {
      database: { generateId: 'serial' },
      useSecureCookies: !!cfg.session.cookie.secure,
      // Адрес клиента: ручка /api/auth/* кладёт в X-Forwarded-For адрес,
      // которому верит (см. server/api/auth/[...all].ts).
      ipAddress: { ipAddressHeaders: ['x-forwarded-for'] },
    },
    rateLimit: {
      enabled: true,
      storage: 'memory',
      window: 60,
      max: 100,
      // Всё, что проверяет пароль или код 2FA, считает неудачи сама ручка
      // /api/auth/* (server/utils/login-limit.ts): библиотека считала бы и
      // удачные попытки.
      customRules: {
        '/sign-in/username': false,
        '/change-password': false,
        '/two-factor/*': false,
        '/get-session': false,
      },
    },
    databaseHooks: {
      session: {
        create: {
          before: async (session, ctx) => {
            if ((session as { loginMethod?: string }).loginMethod) return
            const sso = ctx?.path?.startsWith('/callback/')
            return { data: { ...session, loginMethod: sso ? 'sso' : 'password' } }
          },
        },
      },
    },
    plugins: [
      username({
        minUsernameLength: 1,
        maxUsernameLength: 64,
        usernameNormalization: false,
        usernameValidator: () => true,
      }),
      twoFactor({
        issuer: TOTP_ISSUER,
        schema: { twoFactor: { modelName: 'twoFactors' } },
      }),
      // Провайдер есть, только когда SSO включено и настроено; без него
      // /auth/authentik отвечает sso_off раньше, чем дойдёт до библиотеки.
      genericOAuth({
        config: sso.enabled && ssoConfigured(sso)
          ? [{
          providerId: SSO_PROVIDER,
          discoveryUrl: sso.discoveryUrl,
          clientId: sso.clientId,
          clientSecret: sso.clientSecret,
          scopes: ['openid', 'profile', 'email'],
          pkce: true,
          ...(sso.redirectUrl ? { redirectURI: sso.redirectUrl } : {}),
          disableImplicitSignUp: true,
          disableSignUp: true,
          mapProfileToUser: async (profile) => {
            const sub = String(profile.sub ?? '')
            const preferred = profile.preferred_username as string | undefined
            try {
              const user = await resolveSsoUser(sub, preferred)
              useLogger().info({ sub, username: user.username }, 'sso: вход через Authentik')
              return { email: user.email, emailVerified: true, name: user.name }
            }
            catch (err) {
              if (!(err instanceof SsoDenied)) throw err
              // Причину пишем в лог, наружу только код: в браузере незачем
              // знать, есть ли в панели такая учётка.
              useLogger().warn({ sub, preferred, reason: err.message }, 'sso: вход отклонён')
              throw new APIError('FOUND', undefined, { Location: `${LOGIN_FORM}&error=${err.code}` })
            }
          },
        }]
          : [],
      }),
      panelGuard(),
    ],
  })
}

type Auth = ReturnType<typeof buildAuth>
let _auth: Auth | null = null

export function useAuth(): Auth {
  if (!_auth) _auth = buildAuth()
  return _auth
}

/** Пересобрать экземпляр со свежими настройками (SSO из профиля). */
export function resetAuth() {
  _auth = null
}

/** Сессия запроса или 401. */
export async function requireAuth(event: H3Event) {
  const session = await useAuth().api.getSession({ headers: event.headers })
  if (!session) {
    throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
  }
  return {
    id: Number(session.user.id),
    username: String((session.user as { username?: string }).username ?? ''),
  }
}
