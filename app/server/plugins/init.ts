import { createHash, randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { and, eq, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { accounts, adminConfigPassword, sessions, users } from '../database/schema'
import { hashPassword, revokeDisallowedSsoSessions, verifyPassword } from '../utils/auth'
import { loadAnysdaConfig } from '../utils/anysda-config'
import { syncClientPrefsFile } from '../utils/client-prefs'
import { markPanelReady } from '../utils/panel-ready'
import { ControlUnavailable } from '../utils/control'
import { compactDatabase } from '../utils/access-operations'

export default defineNitroPlugin((nitroApp) => {
  // Nitro не ждёт async-плагин: без миграций или админа падаем процессом.
  const ready = initPanel().then(() => {
    markPanelReady()
    useLogger().info('panel init complete')
  }, (err) => {
    useLogger().fatal({ err }, 'panel init failed')
    console.error('panel init failed:', err)
    process.exit(1)
  })
  // Запросы (и /api/version) ждут готовности.
  nitroApp.hooks.hook('request', () => ready)
  return ready
})

async function initPanel() {
  const log = useLogger()
  const cfg = useRuntimeConfig()

  // 1. Verify the deploy executor's migration history before any application write.
  const localDb = useDb()
  const migrationsFolder = resolve(process.cwd(), 'server/database/migrations')
  const journal = JSON.parse(readFileSync(resolve(migrationsFolder, 'meta/_journal.json'), 'utf8')) as {
    entries: { tag: string, when: number }[]
  }
  const applied = await localDb.all<{ hash: string, created_at: number }>(sql`SELECT hash, created_at FROM __drizzle_migrations ORDER BY created_at`)
  if (applied.length !== journal.entries.length || applied.some((row, index) => {
    const migration = journal.entries[index]!
    return row.created_at !== migration.when || row.hash !== createHash('sha256')
      .update(readFileSync(resolve(migrationsFolder, `${migration.tag}.sql`))).digest('hex')
  })) throw new Error('Database schema incompatible. Run the deploy migration executor before starting the panel.')
  log.info('migrations applied by deploy executor; schema verified')

  // Не вышло (занято, нет места под VACUUM) - панель работает, база прежняя.
  await compactDatabase().then((vacuumed) => {
    if (vacuumed) log.info('database compacted')
  }, err => log.warn({ err }, 'database not compacted'))

  if (cfg.exitTags) await syncClientPrefsFile().catch((error) => {
    if (!(error instanceof ControlUnavailable)) throw error
    log.warn(error.message)
  })

  // 2. Seed/sync admin user from config.yaml (idempotent restore)
  const yamlConfig = loadAnysdaConfig()
  const desiredUser = yamlConfig?.admin?.user ?? process.env.NUXT_ADMIN_USER ?? 'admin'
  let desiredPassword = yamlConfig?.admin?.password ?? process.env.NUXT_ADMIN_PASSWORD ?? ''

  const existing = await localDb.select().from(users).limit(1)

  if (existing.length === 0) {
    if (!desiredPassword) {
      desiredPassword = randomBytes(13).toString('base64url').slice(0, 18)
      const pwdPath = '/var/lib/anysda-vpn2/admin-password.txt'
      try {
        const dir = dirname(pwdPath)
        if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
        writeFileSync(pwdPath, desiredPassword, { mode: 0o600 })
        log.warn({ pwdPath }, 'generated admin password — saved to file')
      }
      catch {
        throw new Error(`Cannot save generated admin password to ${pwdPath}`)
      }
    }
    const passwordHash = await hashPassword(desiredPassword)
    await localDb.transaction(async (tx) => {
      // Синтетическая почта привязана к id, а он известен только после вставки.
      const [row] = await tx.insert(users).values({
        username: desiredUser,
        name: desiredUser,
        email: `new-${randomBytes(8).toString('hex')}@panel.invalid`,
      }).returning({ id: users.id })
      const id = row!.id
      await tx.update(users).set({ email: `u${id}@panel.invalid` }).where(eq(users.id, id))
      await tx.insert(accounts).values({
        accountId: String(id),
        providerId: 'credential',
        userId: id,
        password: passwordHash,
      })
      if (yamlConfig?.admin?.password) await tx.insert(adminConfigPassword).values({ userId: id, hash: passwordHash })
    })
    log.info({ username: desiredUser }, 'admin user created')
  }
  else {
    const current = existing[0]!
    const changed: string[] = []
    if (yamlConfig?.admin?.user && current.username !== yamlConfig.admin.user) {
      await localDb.update(users)
        .set({ username: yamlConfig.admin.user, name: yamlConfig.admin.user, updatedAt: new Date() })
        .where(eq(users.id, current.id))
      changed.push('username')
    }
    // Пароль из конфига применяется, только когда сменили сам конфиг (сравнение
    // с последним применённым), иначе смена в панели откатывалась бы на каждом
    // перезапуске. Вместе с ним рвутся все сессии.
    const yamlPassword = yamlConfig?.admin?.password
    if (yamlPassword) {
      const [applied] = await localDb.select().from(adminConfigPassword)
        .where(eq(adminConfigPassword.userId, current.id)).limit(1)
      if (!applied || !(await verifyPassword(applied.hash, yamlPassword))) {
        const [credential] = await localDb.select().from(accounts)
          .where(and(eq(accounts.userId, current.id), eq(accounts.providerId, 'credential')))
          .limit(1)
        let hash = credential?.password ?? ''
        if (!hash || !(await verifyPassword(hash, yamlPassword))) {
          hash = await hashPassword(yamlPassword)
          if (credential) {
            await localDb.update(accounts).set({ password: hash, updatedAt: new Date() }).where(eq(accounts.id, credential.id))
          }
          else {
            await localDb.insert(accounts).values({ accountId: String(current.id), providerId: 'credential', userId: current.id, password: hash })
          }
          await localDb.delete(sessions).where(eq(sessions.userId, current.id))
          changed.push('password')
        }
        await localDb.insert(adminConfigPassword).values({ userId: current.id, hash })
          .onConflictDoUpdate({ target: adminConfigPassword.userId, set: { hash } })
      }
    }
    if (changed.length > 0) log.info({ keys: changed }, 'admin user synced from config.yaml')
  }

  // 3. Список SSO мог смениться, пока панель стояла.
  const revoked = await revokeDisallowedSsoSessions()
  if (revoked > 0) log.info({ count: revoked }, 'sso sessions revoked: sub no longer allowed')
}
