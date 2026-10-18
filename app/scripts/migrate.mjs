// Explicit deploy executor. Invoke under flock on the shared database directory.
// Application startup only verifies the resulting history; it never calls this.
import { createHash } from 'node:crypto'
import { readFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

const filename = process.env.NUXT_DATABASE_URL ?? 'file:./local.db'
if (!filename.startsWith('file:')) throw new Error('Only a local SQLite database is supported')
const path = resolve(filename.slice(5))
mkdirSync(dirname(path), { recursive: true })
const folder = resolve('server/database/migrations')
const entries = JSON.parse(readFileSync(`${folder}/meta/_journal.json`, 'utf8')).entries
const migrations = entries.map((entry) => {
  const sql = readFileSync(`${folder}/${entry.tag}.sql`, 'utf8')
  return { when: entry.when, hash: createHash('sha256').update(sql).digest('hex'), sql }
})
const db = new DatabaseSync(path)
try {
  db.exec('PRAGMA busy_timeout=30000; BEGIN IMMEDIATE')
  const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()
  if (tables.length && !tables.some(table => table.name === '__drizzle_migrations')) {
    throw new Error('Migration history missing; refusing to change existing database')
  }
  db.exec('CREATE TABLE IF NOT EXISTS __drizzle_migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT NOT NULL, created_at NUMERIC)')
  const applied = db.prepare('SELECT hash, created_at FROM __drizzle_migrations ORDER BY created_at').all()
  if (applied.some((row, index) => row.hash !== migrations[index]?.hash || row.created_at !== migrations[index]?.when)) {
    throw new Error('Incompatible database history; migration refused')
  }
  for (const migration of migrations.slice(applied.length)) {
    db.exec(migration.sql)
    db.prepare('INSERT INTO __drizzle_migrations (hash, created_at) VALUES (?, ?)').run(migration.hash, migration.when)
  }
  db.exec('COMMIT')
  console.log(`database migrations: ${applied.length} -> ${migrations.length}; committed by deploy executor`)
}
catch (error) {
  db.exec('ROLLBACK')
  throw error
}
finally {
  db.close()
}
