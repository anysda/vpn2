import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { eq, sql } from 'drizzle-orm'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import type { SQLiteUpdateSetSource } from 'drizzle-orm/sqlite-core'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { users } from '../database/schema'
import { hashAdminPassword, verifyAdminPassword } from '../utils/auth'
import { loadAnysdaConfig } from '../utils/anysda-config'

function generatePassword(): string {
  return randomBytes(13).toString('base64url').slice(0, 18)
}

export default defineNitroPlugin(async () => {
  // Nitro не ждёт async-плагин: брошенная здесь ошибка уходила в
  // unhandledRejection, а панель продолжала отвечать без миграций или с
  // несинхронизированным админом. Падаем процессом — контейнер это покажет,
  // а 30-frontend напечатает журнал.
  try {
    await initPanel()
  }
  catch (err) {
    useLogger().fatal({ err }, 'panel init failed')
    console.error('panel init failed:', err)
    process.exit(1)
  }
})

async function initPanel() {
  const log = useLogger()
  const cfg = useRuntimeConfig()

  // Note: session cookie's `Secure` flag is handled by a build-time sed patch
  // in Dockerfile against /app/.output/.../nitro.mjs (h3's DEFAULT_COOKIE).
  // Reason: nuxt-auth-utils' module-time defu strips arbitrary keys we set
  // through nuxt.config.runtimeConfig.session, so we can't toggle Secure at
  // config or runtime — only at compile time.

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
    const updates: SQLiteUpdateSetSource<typeof users> = {}
    if (yamlConfig?.admin?.user && current.username !== yamlConfig.admin.user) {
      updates.username = yamlConfig.admin.user
    }
    // Пароль из конфига перезаписывается только при настоящей смене, и вместе
    // с ним поднимается поколение сессий, как при смене в панели (VPN2-49):
    // старые сессии со старым паролем больше не пускают. Тот же пароль на
    // каждом перезапуске сессии не трогает.
    const yamlPassword = yamlConfig?.admin?.password
    if (yamlPassword && !(await verifyAdminPassword(current.passwordHash, yamlPassword))) {
      updates.passwordHash = await hashAdminPassword(yamlPassword)
      updates.sessionVersion = sql`${users.sessionVersion} + 1`
    }
    if (Object.keys(updates).length > 0) {
      updates.updatedAt = new Date()
      await localDb.update(users).set(updates).where(eq(users.id, current.id))
      log.info({ keys: Object.keys(updates) }, 'admin user synced from config.yaml')
    }
  }
}
