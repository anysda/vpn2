import { createHash, randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { and, eq, isNotNull, isNull, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { generateRandomString, symmetricEncrypt } from 'better-auth/crypto'
import { accounts, sessions, twoFactors, users } from '../database/schema'
import { hashPassword, useAuth, verifyPassword } from '../utils/auth'
import { loadAnysdaConfig } from '../utils/anysda-config'
import { syncClientPrefsFile } from '../utils/client-prefs'
import { markPanelReady } from '../utils/panel-ready'
import { ControlUnavailable } from '../utils/control'

function generatePassword(): string {
  return randomBytes(13).toString('base64url').slice(0, 18)
}

export default defineNitroPlugin((nitroApp) => {
  // Nitro не ждёт async-плагин: брошенная здесь ошибка уходила в
  // unhandledRejection, а панель продолжала отвечать без миграций или с
  // несинхронизированным админом. Падаем процессом — контейнер это покажет,
  // а 30-frontend напечатает журнал.
  const ready = initPanel().then(() => {
    markPanelReady()
    useLogger().info('panel init complete')
  }, (err) => {
    useLogger().fatal({ err }, 'panel init failed')
    console.error('panel init failed:', err)
    process.exit(1)
  })
  // Обработчик request Nitro ждёт, в отличие от async-плагина. В том числе
  // /api/version не должен сообщать о готовности до создания администратора.
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
      desiredPassword = generatePassword()
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
      // Почта учётки синтетическая и привязана к id (см. schema.ts), а id
      // известен только после вставки.
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
    // Пароль из конфига перезаписывается только при настоящей смене, и вместе
    // с ним рвутся все сессии, как при смене в панели (VPN2-49). Тот же пароль
    // на каждом перезапуске сессии не трогает.
    const yamlPassword = yamlConfig?.admin?.password
    if (yamlPassword) {
      const [credential] = await localDb.select().from(accounts)
        .where(and(eq(accounts.userId, current.id), eq(accounts.providerId, 'credential')))
        .limit(1)
      if (!credential?.password || !(await verifyPassword(credential.password, yamlPassword))) {
        const password = await hashPassword(yamlPassword)
        if (credential) {
          await localDb.update(accounts).set({ password, updatedAt: new Date() }).where(eq(accounts.id, credential.id))
        }
        else {
          await localDb.insert(accounts).values({ accountId: String(current.id), providerId: 'credential', userId: current.id, password })
        }
        await localDb.delete(sessions).where(eq(sessions.userId, current.id))
        changed.push('password')
      }
    }
    if (changed.length > 0) log.info({ keys: changed }, 'admin user synced from config.yaml')
  }

  // 3. 2FA, включённая до better-auth: секрет остаётся в users.totp_secret и
  // проверяется хуком в utils/auth.ts, а библиотеке нужна своя строка, чтобы
  // она спрашивала код при входе. Её секрет случайный: коды человека он не
  // проверяет, только код, который подставляет хук.
  const legacy = await localDb.select({ id: users.id }).from(users)
    .leftJoin(twoFactors, eq(twoFactors.userId, users.id))
    .where(and(isNotNull(users.legacyTotpSecret), isNull(twoFactors.id)))
  if (legacy.length > 0) {
    const { secretConfig } = await useAuth().$context
    for (const { id } of legacy) {
      await localDb.insert(twoFactors).values({
        userId: id,
        secret: await symmetricEncrypt({ key: secretConfig, data: generateRandomString(32) }),
        backupCodes: await symmetricEncrypt({ key: secretConfig, data: '[]' }),
        verified: true,
      })
      await localDb.update(users).set({ twoFactorEnabled: true }).where(eq(users.id, id))
    }
    log.info({ count: legacy.length }, '2FA migrated to better-auth')
  }
}
