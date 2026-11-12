import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { H3Event } from 'h3'
import { pino, type Logger } from 'pino'
import type { ZodType } from 'zod'

let logger: Logger | null = null
export function useLogger() {
  return (logger ??= pino({ level: process.env.LOG_LEVEL ?? 'info' }))
}

let ready = false
export const markPanelReady = () => { ready = true }
export const isPanelReady = () => ready

/** Файл настроек панели в /var/lib/anysda-vpn2/runtime; нет или битый - null. */
export function readJsonFile<T>(path: string): T | null {
  try {
    return existsSync(path) ? JSON.parse(readFileSync(path, 'utf-8')) as T : null
  }
  catch (err) {
    useLogger().warn({ err, path }, 'runtime file unreadable')
    return null
  }
}

export function writeJsonFile(path: string, data: unknown) {
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 })
}

const LOOPBACK = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])
export const isLoopbackAddr = (ip: string | undefined) => !!ip && LOOPBACK.has(ip)

type AdminUser = Awaited<ReturnType<typeof requireAuth>>

/** Маршрут панели: только с сессией администратора. Ключ таблиц `server/resources` - `МЕТОД /путь` под `/api`. */
export const admin = <T>(handler: (event: H3Event, user: AdminUser) => T) =>
  async (event: H3Event) => handler(event, await requireAuth(event))

export const bot = <T>(handler: (event: H3Event) => T) => async (event: H3Event) => {
  requireBotAuth(event)
  return handler(event)
}

export const validId = (id: number) => Number.isSafeInteger(id) && id > 0

export function idParam(event: H3Event, name = 'id'): number {
  const id = Number(getRouterParam(event, name))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  return id
}

/** Номера записей из адреса для операций доступа: каждый - целое больше нуля. */
export function recordIds<N extends string>(event: H3Event, ...names: N[]): Record<N, number> {
  const ids = names.map(name => Number(getRouterParam(event, name)))
  if (!ids.every(validId)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  return Object.fromEntries(names.map((name, i) => [name, ids[i]])) as Record<N, number>
}

export function accepted<T>(event: H3Event, result: T): T {
  setResponseStatus(event, 202)
  return result
}

export const notFound = () => createError({ statusCode: 404, statusMessage: 'not_found' })

// Подписи полей для человека; поля без подписи называются как в запросе.
export const FIELD_LABELS: Record<string, string> = {
  name: 'Имя',
  value: 'Правило',
  outbound: 'Выход',
  username: 'Логин',
  password: 'Пароль',
  currentPassword: 'Текущий пароль',
  newPassword: 'Новый пароль',
  code: 'Код',
  totpCode: 'Код',
  expiresAt: 'Срок действия',
  deviceLimit: 'Лимит устройств',
}

/** Тело запроса по схеме zod: код в `statusMessage`, русский текст в `message`. */
export async function readBodyAs<T>(event: Parameters<typeof readBody>[0], schema: ZodType<T>): Promise<T> {
  let body: unknown
  try {
    body = await readBody(event, { strict: true })
  }
  catch (e) {
    if (isError(e) && e.statusCode === 400) {
      throw createError({ statusCode: 400, statusMessage: 'invalid_json', message: 'Запрос не в формате JSON' })
    }
    throw e
  }
  const r = schema.safeParse(body)
  if (r.success) return r.data
  const key = r.error.issues[0]?.path[0]
  throw createError({
    statusCode: 400,
    statusMessage: 'invalid_body',
    message: key === undefined ? 'Проверьте данные запроса' : `Проверьте поле «${FIELD_LABELS[String(key)] ?? String(key)}»`,
  })
}

/** Очередь: следующий вызов начинается после конца предыдущего, ошибка не рвёт цепочку. */
export function serialQueue() {
  let tail: Promise<unknown> = Promise.resolve()
  return <T>(fn: () => Promise<T>): Promise<T> => {
    const run = tail.then(fn)
    tail = run.catch(() => {})
    return run
  }
}

/**
 * Перегенерация конфига: пока прогон идёт, новые вызовы ждут один следующий, который читает
 * базу заново. Файл не пишут два прогона сразу, и последним не ложится устаревший снимок.
 */
export function coalesceRuns(fn: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | null = null
  let next: Promise<void> | null = null

  const start = (): Promise<void> => {
    const p = fn().finally(() => {
      if (running === p) running = null
    })
    running = p
    return p
  }

  return () => {
    if (!running) return start()
    if (!next) {
      next = running.catch(() => {}).then(() => {
        next = null
        return start()
      })
    }
    return next
  }
}
