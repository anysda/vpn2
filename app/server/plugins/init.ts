import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { eq } from 'drizzle-orm'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { users } from '../database/schema'
import { hashAdminPassword } from '../utils/auth'
import { loadAnysdaConfig } from '../utils/anysda-config'

function generatePassword(): string {
  return randomBytes(13).toString('base64url').slice(0, 18)
}

export default defineNitroPlugin(async () => {
  const log = useLogger()
  const cfg = useRuntimeConfig()

  // 1. Run migrations
  const dbUrl = cfg.databaseUrl
  const localClient = createClient({ url: dbUrl })
  const localDb = drizzle(localClient)

  const migrationsFolder = resolve(process.cwd(), 'server/database/migrations')
  if (existsSync(migrationsFolder)) {
    try {
      await migrate(localDb, { migrationsFolder })
      log.info({ dbUrl }, 'migrations applied')
    }
    catch (err) {
      log.error({ err }, 'migration failed')
      throw err
    }
  }
  else {
    log.warn({ migrationsFolder }, 'migrations folder missing, skipping')
  }

  // 2. Seed/sync admin user from config.yaml (idempotent restore)
  const yamlConfig = loadAnysdaConfig()
  const desiredUser = yamlConfig?.admin?.user ?? process.env.NUXT_ADMIN_USER ?? 'admin'
  let desiredPassword = yamlConfig?.admin?.password ?? process.env.NUXT_ADMIN_PASSWORD ?? ''

  const existing = await localDb.select().from(users).limit(1)

  if (existing.length === 0) {
    if (!desiredPassword) {
      desiredPassword = generatePassword()
      const pwdPath = '/etc/anysda/admin-password.txt'
      try {
        const dir = dirname(pwdPath)
        if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
        writeFileSync(pwdPath, desiredPassword, { mode: 0o600 })
        log.warn({ pwdPath }, 'generated admin password — saved to file')
      }
      catch {
        log.warn({ password: desiredPassword }, 'GENERATED ADMIN PASSWORD (could not write file) — save it now')
      }
    }
    await localDb.insert(users).values({
      username: desiredUser,
      passwordHash: await hashAdminPassword(desiredPassword),
    })
    log.info({ username: desiredUser }, 'admin user created')
  }
  else {
    const current = existing[0]!
    const updates: Partial<typeof users.$inferInsert> = {}
    if (yamlConfig?.admin?.user && current.username !== yamlConfig.admin.user) {
      updates.username = yamlConfig.admin.user
    }
    if (yamlConfig?.admin?.password) {
      updates.passwordHash = await hashAdminPassword(yamlConfig.admin.password)
    }
    if (Object.keys(updates).length > 0) {
      updates.updatedAt = new Date()
      await localDb.update(users).set(updates).where(eq(users.id, current.id))
      log.info({ keys: Object.keys(updates) }, 'admin user synced from config.yaml')
    }
  }
})
