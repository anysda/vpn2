import { createConnection, type Socket } from 'node:net'
import { createHash } from 'node:crypto'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { collectorState } from '../database/schema'

export class ControlUnavailable extends Error {
  constructor() { super('Нет связи с локальным сервисом управления VPN') }
}

// Сервис принимает соединения по одному, очередь listen — Backlog=16 в
// vpn2-control.socket. Сверх неё connect() к Unix-сокету сразу получает
// EAGAIN, и запрос пропадал без попытки. Панель держит не больше 8 соединений,
// остальные ждут здесь; срок 35 с считается от вызова, вместе с ожиданием.
const CONNECTIONS = 8
let open = 0
const waiting: Array<() => void> = []

function release() {
  const next = waiting.shift()
  if (next) next()
  else open--
}

/** API v1: bounded JSON lines, no commands or paths supplied by callers. */
export function control<T>(method: string, params: Record<string, unknown>): Promise<T> {
  return new Promise((resolve, reject) => {
    let socket: Socket | undefined
    let queued: (() => void) | undefined
    const chunks: Buffer[] = []
    let size = 0
    let settled = false
    const finish = (error?: Error, result?: T) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (queued) waiting.splice(waiting.indexOf(queued), 1)
      if (socket) {
        socket.destroy()
        release()
      }
      if (error) reject(error)
      else resolve(result!)
    }
    const timer = setTimeout(() => finish(new ControlUnavailable()), 35_000)
    const connect = () => {
      queued = undefined
      socket = createConnection(process.env.VPN2_CONTROL_SOCKET || '/run/vpn2-control/control.sock')
      socket.on('error', () => finish(new ControlUnavailable()))
      socket.on('close', () => finish(new ControlUnavailable()))
      socket.on('connect', () => {
        const request = JSON.stringify({ api: 1, method, params }) + '\n'
        if (Buffer.byteLength(request) > 1024 * 1024) return finish(new Error('Запрос управления слишком большой'))
        socket!.write(request)
      })
      socket.on('data', (chunk: Buffer) => {
        size += chunk.length
        if (size > 1024 * 1024) return finish(new ControlUnavailable())
        chunks.push(chunk)
        if (!chunk.includes(10)) return
        try {
          const reply = JSON.parse(Buffer.concat(chunks).toString('utf8'))
          if (reply.api !== 1) return finish(new ControlUnavailable())
          if (!reply.ok) return finish(new Error('Сервис управления не смог выполнить запрос'))
          finish(undefined, reply.result as T)
        }
        catch { finish(new ControlUnavailable()) }
      })
    }
    if (open < CONNECTIONS) {
      open++
      connect()
    }
    else {
      queued = connect
      waiting.push(connect)
    }
  })
}

let tail: Promise<unknown> = Promise.resolve()

/** Persist a separate version for each service resource, before sending it. */
export function controlApply<T>(method: string, params: Record<string, unknown>): Promise<T> {
  const run = tail.then(async () => {
    const name = `control:${method}:${params.device_id ?? params.resource ?? ''}`
    const digest = createHash('sha256').update(JSON.stringify(params)).digest('hex')
    const db = useDb()
    const version = await db.transaction(async (tx) => {
      const [row] = await tx.select().from(collectorState).where(eq(collectorState.name, name))
      const previous = row ? JSON.parse(row.value) as { digest: string, version: number } : null
      const version = previous?.digest === digest ? previous.version : (previous?.version ?? 0) + 1
      const value = JSON.stringify({ digest, version })
      await tx.insert(collectorState).values({ name, value }).onConflictDoUpdate({ target: collectorState.name, set: { value } })
      return version
    }, { behavior: 'immediate' })
    return control<T>(method, { ...params, version })
  })
  tail = run.catch(() => {})
  return run
}
