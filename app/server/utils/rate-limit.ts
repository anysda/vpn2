import type { H3Event } from 'h3'

/**
 * In-memory sliding-window rate-limit for unauthenticated endpoints (login,
 * bot-link). НЕ persists через рестарт — для serious-grade brute-force
 * нужна persisted users.failed_attempts (см. SECURITY-AUDIT-2026-06-01.md M9).
 */
type Bucket = { hits: number[]; lockedUntil: number }
const buckets = new Map<string, Bucket>()

function clientKey(event: H3Event, scope: string): string {
  const ip
    = getRequestHeader(event, 'x-forwarded-for')?.split(',')[0]?.trim()
    ?? event.node.req.socket.remoteAddress
    ?? 'unknown'
  return `${scope}:${ip}`
}

interface Opts {
  scope: string
  maxAttempts: number
  windowMs: number
  lockoutMs: number
}

export function rateLimitGuard(event: H3Event, opts: Opts): void {
  const key = clientKey(event, opts.scope)
  const now = Date.now()
  const b = buckets.get(key) ?? { hits: [], lockedUntil: 0 }
  if (now < b.lockedUntil) {
    throw createError({
      statusCode: 429,
      statusMessage: 'too_many_requests',
      data: { retryAfterSec: Math.ceil((b.lockedUntil - now) / 1000) },
    })
  }
  b.hits = b.hits.filter(t => now - t < opts.windowMs)
  buckets.set(key, b)
}

export function rateLimitRecordFailure(event: H3Event, opts: Opts): void {
  const key = clientKey(event, opts.scope)
  const now = Date.now()
  const b = buckets.get(key) ?? { hits: [], lockedUntil: 0 }
  b.hits = b.hits.filter(t => now - t < opts.windowMs)
  b.hits.push(now)
  if (b.hits.length >= opts.maxAttempts) {
    b.lockedUntil = now + opts.lockoutMs
    b.hits = []
  }
  buckets.set(key, b)
}

export function rateLimitClear(event: H3Event, opts: Opts): void {
  buckets.delete(clientKey(event, opts.scope))
}
