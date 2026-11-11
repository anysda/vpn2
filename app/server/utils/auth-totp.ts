import { base32 } from '@better-auth/utils/base32'
import { createOTP } from '@better-auth/utils/otp'
import { symmetricDecrypt } from 'better-auth/crypto'
import { and, eq, isNull, lt, or } from 'drizzle-orm'
import { useDb } from '../database/client'
import * as schema from '../database/schema'

export const TOTP_PERIOD = 30
export const TOTP_ISSUER = 'anysda-vpn2'

type OtpKey = string | CryptoKey
// В типах createOTP ключ только строка, но CryptoKey проходит как есть.
export const otp = (key: OtpKey) => createOTP(key as string, { period: TOTP_PERIOD, digits: 6 })

/** Секрет, заведённый до better-auth: base32 от сырых байт ключа. */
async function legacyKey(secret: string): Promise<CryptoKey> {
  const bytes = base32.decode(secret.toUpperCase().replace(/[\s=]/g, ''))
  return crypto.subtle.importKey('raw', bytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign'])
}

/** check: чем проверять код человека; library: секрет библиотеки. */
interface TotpKeys { check: OtpKey, library: string }

export async function totpKeys(secretConfig: unknown, userId: number): Promise<TotpKeys | null> {
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

/** Проверяет код (±1 шаг) и гасит его условным update: из параллельных запросов пройдёт один. */
export async function consumeCode(userId: number, key: OtpKey, code: unknown): Promise<number | null> {
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
