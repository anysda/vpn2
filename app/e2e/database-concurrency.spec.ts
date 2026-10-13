import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { once } from 'node:events'
import { sql } from 'drizzle-orm'
import { test, expect } from '@playwright/test'
import * as schema from '../server/database/schema'
import { serverModule } from './support/server-module'

test('SQLite: внешний writer и reader не срывают запрос панели', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'panel-lock-'))
  const path = join(directory, 'panel.sqlite')
  const { useDb } = serverModule<typeof import('../server/database/client')>('server/database/client.ts', {
    './schema': schema,
  }, { useRuntimeConfig: () => ({ databaseUrl: `file:${path}` }) })
  const db = useDb()
  const failures: string[] = []
  try {
    await db.run(sql`CREATE TABLE probe (value INTEGER)`)
    console.log('SQLite connection:', await db.get(sql`PRAGMA journal_mode`), await db.get(sql`PRAGMA busy_timeout`))
    for (const mode of ['writer', 'reader']) {
      for (let i = 0; i < 5; i++) {
        const child = spawn(process.execPath, ['--input-type=module', '-e', `
          import { DatabaseSync } from 'node:sqlite';
          const db = new DatabaseSync(process.argv[1]);
          db.exec('PRAGMA busy_timeout=5000');
          db.exec(process.argv[2] === 'writer' ? 'BEGIN IMMEDIATE; INSERT INTO probe VALUES (1)' : 'BEGIN; SELECT * FROM probe');
          console.log('locked');
          setTimeout(() => { db.exec('COMMIT'); db.close(); }, 250);
        `, path, mode], { stdio: ['ignore', 'pipe', 'pipe'] })
        const done = once(child, 'exit')
        try {
          await once(child.stdout, 'data')
          await db.run(sql`INSERT INTO probe VALUES (2)`)
        }
        catch (error) { failures.push(`${mode}: ${String((error as Error).cause ?? error)}`) }
        finally { expect((await done)[0]).toBe(0) }
      }
    }
    console.log(`SQLite: ${failures.length}/10 запросов упало`, failures)
    expect(failures).toEqual([])
  }
  finally {
    db.$client.close()
    rmSync(directory, { recursive: true, force: true })
  }
})

test('SQLite: параллельные транзакции и запросы изолированы, rollback освобождает очередь', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'panel-transactions-'))
  const { useDb } = serverModule<typeof import('../server/database/client')>('server/database/client.ts', {
    './schema': schema,
  }, { useRuntimeConfig: () => ({ databaseUrl: `file:${join(directory, 'panel.sqlite')}` }) })
  const db = useDb()
  try {
    await db.run(sql`CREATE TABLE probe (value INTEGER)`)
    const results = await Promise.allSettled(Array.from({ length: 20 }, (_, i) => db.transaction(async (tx) => {
      await tx.run(sql`INSERT INTO probe VALUES (${i})`)
      await new Promise(resolve => setTimeout(resolve, 5))
      if (i % 2) throw new Error('intentional rollback')
    })))
    const unexpected = results.filter(r => r.status === 'rejected' && r.reason.message !== 'intentional rollback')
    console.log(`SQLite: ${unexpected.length}/20 неожиданных ошибок транзакций`)
    expect(unexpected).toEqual([])
    expect(await db.all(sql`SELECT value FROM probe ORDER BY value`)).toEqual(Array.from({ length: 10 }, (_, i) => ({ value: i * 2 })))
    // Запрос вне транзакции не должен увидеть/изменить её незавершённую запись.
    let started!: () => void
    const ready = new Promise<void>(resolve => { started = resolve })
    const transaction = db.transaction(async (tx) => {
      await tx.run(sql`INSERT INTO probe VALUES (100)`)
      started()
      await new Promise(resolve => setTimeout(resolve, 50))
      throw new Error('intentional rollback')
    }).catch(error => { expect(error.message).toBe('intentional rollback') })
    await ready
    expect(await db.all(sql`SELECT value FROM probe WHERE value = 100`)).toEqual([])
    await transaction
  }
  finally {
    db.$client.close()
    rmSync(directory, { recursive: true, force: true })
  }
})
