import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { beforeAll, describe, expect, it } from 'vitest'
import { clients, devices } from '../database/schema'
import { clientsNewestFirst, devicesOldestFirst } from './list-order'

const serverDir = resolve(__dirname, '..')

async function makeDb() {
  const db = drizzle(createClient({ url: ':memory:' }))
  await migrate(db, { migrationsFolder: resolve(serverDir, 'database/migrations') })
  return db
}

const at = (seconds: number) => new Date(seconds * 1000)

describe('порядок списков при одинаковом created_at', () => {
  let db: Awaited<ReturnType<typeof makeDb>>
  let clientId: number

  beforeAll(async () => {
    db = await makeDb()
    for (let i = 0; i < 5; i++) {
      await db.insert(clients).values({ name: `same-${i}`, password: `pw-same-${i}`, createdAt: at(2000) })
    }
    await db.insert(clients).values({ name: 'earlier', password: 'pw-earlier', createdAt: at(1999) })
    const [owner] = await db.select().from(clients).limit(1)
    clientId = owner!.id
    for (let i = 0; i < 5; i++) {
      await db.insert(devices).values({ clientId, name: `dev-same-${i}`, createdAt: at(2000) })
    }
    await db.insert(devices).values({ clientId, name: 'dev-later', createdAt: at(2001) })
  })

  it('клиенты: новые первыми, при равном времени id по убыванию, 20 повторов', async () => {
    for (let run = 0; run < 20; run++) {
      const rows = await db.select().from(clients).orderBy(...clientsNewestFirst)
      const ids = rows.map(r => r.id)
      const sameIds = ids.slice(0, 5)
      expect(sameIds).toEqual([...sameIds].sort((a, b) => b - a))
      expect(rows.slice(0, 5).every(r => r.name.startsWith('same-'))).toBe(true)
      expect(rows[5]!.name).toBe('earlier')
      expect(ids).toHaveLength(6)
    }
  })

  it('устройства: старые первыми, при равном времени id по возрастанию', async () => {
    for (let run = 0; run < 20; run++) {
      const rows = await db.select().from(devices).orderBy(...devicesOldestFirst)
      const ids = rows.map(r => r.id)
      const sameIds = ids.slice(0, 5)
      expect(sameIds).toEqual([...sameIds].sort((a, b) => a - b))
      expect(rows.slice(0, 5).every(r => r.name.startsWith('dev-same-'))).toBe(true)
      expect(rows[5]!.name).toBe('dev-later')
      expect(ids).toHaveLength(6)
    }
  })
})

describe('сторож мест вызова', () => {
  const files = (readdirSync(serverDir, { recursive: true }) as string[])
    .map(String)
    .filter(f => f.endsWith('.ts') && !f.endsWith('.test.ts'))

  it('просмотрено больше 50 файлов', () => {
    expect(files.length).toBeGreaterThan(50)
  })

  it('нет сортировки только по created_at', () => {
    const banned = ['orderBy(desc(clients.createdAt))', 'orderBy(asc(devices.createdAt))']
    const offenders: string[] = []
    for (const file of files) {
      const text = readFileSync(join(serverDir, file), 'utf8')
      for (const needle of banned) {
        if (text.includes(needle)) offenders.push(`${file}: ${needle}`)
      }
    }
    expect(offenders, `сортировка без второго ключа:\n${offenders.join('\n')}`).toEqual([])
  })
})
