import { createHash, randomBytes } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { and, eq, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { accounts, adminConfigPassword, sessions, users } from '../database/schema'

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
  const configUser = yamlConfig?.admin?.user
  const configPassword = yamlConfig?.admin?.password
  const [current] = await localDb.select().from(users).limit(1)

  if (!current) {
    const username = configUser ?? process.env.NUXT_ADMIN_USER ?? 'admin'
    const password = configPassword || process.env.NUXT_ADMIN_PASSWORD || await generatedAdminPassword()
    const passwordHash = await hashPassword(password)
    await localDb.transaction(async (tx) => {
      // Синтетическая почта привязана к id, а он известен только после вставки.
      const [row] = await tx.insert(users)
        .values({ username, name: username, email: `new-${randomBytes(8).toString('hex')}@panel.invalid` }).returning({ id: users.id })
      const id = row!.id
      await tx.update(users).set({ email: `u${id}@panel.invalid` }).where(eq(users.id, id))
      await tx.insert(accounts).values({ accountId: String(id), providerId: 'credential', userId: id, password: passwordHash })
      if (configPassword) await tx.insert(adminConfigPassword).values({ userId: id, hash: passwordHash })
    })
    log.info({ username }, 'admin user created')
  }
  else {
    const changed: string[] = []
    if (configUser && current.username !== configUser) {
      await localDb.update(users).set({ username: configUser, name: configUser, updatedAt: new Date() }).where(eq(users.id, current.id))
      changed.push('username')
    }
    if (configPassword && await applyConfigPassword(current.id, configPassword)) changed.push('password')
    if (changed.length > 0) log.info({ keys: changed }, 'admin user synced from config.yaml')
  }

  // 3. Список SSO мог смениться, пока панель стояла.
  const revoked = await revokeDisallowedSsoSessions()
  if (revoked > 0) log.info({ count: revoked }, 'sso sessions revoked: sub no longer allowed')
}

/** Пароля нет ни в конфиге, ни в env: случайный, в файл только для root. */
async function generatedAdminPassword(): Promise<string> {
  const password = randomBytes(13).toString('base64url').slice(0, 18)
  const path = '/var/lib/anysda-vpn2/admin-password.txt'
  try {
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, password, { mode: 0o600 })
  }
  catch {
    throw new Error(`Cannot save generated admin password to ${path}`)
  }
  useLogger().warn({ pwdPath: path }, 'generated admin password — saved to file')
  return password
}

/**
 * Пароль из конфига применяется, только когда сменили сам конфиг (сравнение с последним
 * применённым), иначе смена в панели откатывалась бы на каждом перезапуске. Вместе с ним рвутся все сессии.
 */
async function applyConfigPassword(userId: number, password: string): Promise<boolean> {
  const db = useDb()
  const [applied] = await db.select().from(adminConfigPassword).where(eq(adminConfigPassword.userId, userId)).limit(1)
  if (applied && await verifyPassword(applied.hash, password)) return false
  const [credential] = await db.select().from(accounts)
    .where(and(eq(accounts.userId, userId), eq(accounts.providerId, 'credential'))).limit(1)
  const same = !!credential?.password && await verifyPassword(credential.password, password)
  const hash = same ? credential!.password! : await hashPassword(password)
  if (!same) {
    if (credential) await db.update(accounts).set({ password: hash, updatedAt: new Date() }).where(eq(accounts.id, credential.id))
    else await db.insert(accounts).values({ accountId: String(userId), providerId: 'credential', userId, password: hash })
    await db.delete(sessions).where(eq(sessions.userId, userId))
  }
  await db.insert(adminConfigPassword).values({ userId, hash }).onConflictDoUpdate({ target: adminConfigPassword.userId, set: { hash } })
  return !same
}
