import { sql } from 'drizzle-orm'
import { index, integer, primaryKey, sqliteTable, text, uniqueIndex } from 'drizzle-orm/sqlite-core'
import type { AccessOperation } from '../../shared/access-operation'

const createdAt = integer('created_at', { mode: 'timestamp' }).notNull().default(sql`(strftime('%s','now'))`)
const timestamps = {
  createdAt,
  updatedAt: integer('updated_at', { mode: 'timestamp' }).notNull().default(sql`(strftime('%s','now'))`),
}

/**
 * Учётки панели; вход, сессии, 2FA и SSO ведёт better-auth (utils/auth.ts).
 * Почта синтетическая (`u<id>@panel.invalid`): по ней привязывается вход через Authentik.
 */
export const users = sqliteTable('users', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  username: text('username').notNull().unique(),
  displayUsername: text('display_username'),
  name: text('name').notNull(),
  email: text('email').notNull().unique(),
  emailVerified: integer('email_verified', { mode: 'boolean' }).notNull().default(true),
  image: text('image'),
  twoFactorEnabled: integer('two_factor_enabled', { mode: 'boolean' }).notNull().default(false),
  // Секрет 2FA в base32, его проверяет хук в utils/auth.ts.
  legacyTotpSecret: text('totp_secret'),
  // Последний принятый шаг TOTP: код этого и более раннего шага повторно не принимается.
  totpLastStep: integer('totp_last_step'),
  ...timestamps,
})

/** argon2 последнего применённого admin.password из config.yaml: смену в панели init не путает со сменой в конфиге. */
export const adminConfigPassword = sqliteTable('admin_config_password', {
  userId: integer('user_id').primaryKey().references(() => users.id, { onDelete: 'cascade' }),
  hash: text('hash').notNull(),
})

export const sessions = sqliteTable('sessions', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  token: text('token').notNull().unique(),
  userId: integer('user_id').notNull().references(() => users.id, { onDelete: 'cascade' }),
  expiresAt: integer('expires_at', { mode: 'timestamp' }).notNull(),
  ipAddress: text('ip_address'),
  userAgent: text('user_agent'),
  // 'password' или 'sso' - чем вошли; показывается в профиле.
  loginMethod: text('login_method'),
  ...timestamps,
}, table => [index('sessions_user_idx').on(table.userId)])

/** Способы входа учётки: providerId 'credential' - пароль (argon2), 'authentik' - SSO по sub. */
export const accounts = sqliteTable('accounts', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  accountId: text('account_id').notNull(),
  providerId: text('provider_id').notNull(),
  userId: integer('user_id').notNull().references(() => users.id, { onDelete: 'cascade' }),
  accessToken: text('access_token'),
  refreshToken: text('refresh_token'),
  idToken: text('id_token'),
  accessTokenExpiresAt: integer('access_token_expires_at', { mode: 'timestamp' }),
  refreshTokenExpiresAt: integer('refresh_token_expires_at', { mode: 'timestamp' }),
  scope: text('scope'),
  password: text('password'),
  ...timestamps,
}, table => [
  index('accounts_user_idx').on(table.userId),
  uniqueIndex('accounts_provider_account_unique').on(table.providerId, table.accountId),
])

export const verifications = sqliteTable('verifications', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  identifier: text('identifier').notNull(),
  value: text('value').notNull(),
  expiresAt: integer('expires_at', { mode: 'timestamp' }).notNull(),
  ...timestamps,
}, table => [index('verifications_identifier_idx').on(table.identifier)])

export const twoFactors = sqliteTable('two_factors', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  userId: integer('user_id').notNull().references(() => users.id, { onDelete: 'cascade' }),
  secret: text('secret').notNull(),
  backupCodes: text('backup_codes').notNull(),
  verified: integer('verified', { mode: 'boolean' }).default(true),
  failedVerificationCount: integer('failed_verification_count'),
  lockedUntil: integer('locked_until', { mode: 'timestamp' }),
}, table => [
  index('two_factors_user_idx').on(table.userId),
  index('two_factors_secret_idx').on(table.secret),
])

/** Клиент = человек. Статус не хранится: считается из frozenManual и expiresAt (utils/client-status.ts). */
export const clients = sqliteTable('clients', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  accessVersion: integer('access_version').notNull().default(0),
  deleting: integer('deleting', { mode: 'boolean' }).notNull().default(false),
  name: text('name').notNull(),
  // Фильтрация трафика (AdGuard DNS), наследуется всеми устройствами клиента.
  filterTraffic: integer('filter_traffic', { mode: 'boolean' }).notNull().default(true),
  // null - бессрочно. По истечении клиент заморожен, продление возвращает доступ.
  expiresAt: integer('expires_at', { mode: 'timestamp' }),
  // null - безлимит.
  deviceLimit: integer('device_limit').default(3),
  frozenManual: integer('frozen_manual', { mode: 'boolean' }).notNull().default(false),
  // 20 символов [A-Za-z]; им же клиент привязывает Telegram (диплинк ?start=<password>).
  password: text('password').notNull(),
  tgChatId: integer('tg_chat_id'),
  tgUsername: text('tg_username'),
  // Предпочитаемый экзит (тег узла); null - авто. См. utils/client-prefs.ts.
  preferredExit: text('preferred_exit'),
  ...timestamps,
})

/** Устройство клиента со своими ключами WG/OVPN/IKEv2. */
export const devices = sqliteTable('devices', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  deleting: integer('deleting', { mode: 'boolean' }).notNull().default(false),
  clientId: integer('client_id').notNull().references(() => clients.id, { onDelete: 'cascade' }),
  name: text('name').notNull(),
  wgPrivateKey: text('wg_private_key'),
  wgPublicKey: text('wg_public_key'),
  wgPresharedKey: text('wg_preshared_key'),
  wgIp: text('wg_ip'),
  ovpnCert: text('ovpn_cert'),
  ovpnKey: text('ovpn_key'),
  // Постоянный адрес OpenVPN (ifconfig-push в ccd): по нему роутер узнаёт устройство.
  ovpnIp: text('ovpn_ip'),
  // IKEv2 EAP-MSCHAPv2: логин стабилен, пароль меняется при перевыпуске, адрес статический.
  ikev2Username: text('ikev2_username'),
  ikev2Password: text('ikev2_password'),
  ikev2Ip: text('ikev2_ip'),
  // Накопительный трафик устройства по всем протоколам, байты (utils/traffic-collector.ts).
  rxTotal: integer('rx_total').notNull().default(0),
  txTotal: integer('tx_total').notNull().default(0),
  ...timestamps,
}, table => [
  index('devices_client_idx').on(table.clientId),
  // UNIQUE защищает от гонки параллельных созданий за адрес и логин.
  uniqueIndex('devices_wg_ip_unique').on(table.wgIp),
  uniqueIndex('devices_ikev2_ip_unique').on(table.ikev2Ip),
  uniqueIndex('devices_ovpn_ip_unique').on(table.ovpnIp),
  uniqueIndex('devices_ikev2_username_unique').on(table.ikev2Username),
])

/**
 * Idempotency-Key создания устройства. Каскада нет: повтор после удаления
 * получает отказ, а не новое устройство; с другим телом или клиентом тоже отказ.
 */
export const deviceRequests = sqliteTable('device_requests', {
  idempotencyKey: text('idempotency_key').primaryKey(),
  requestHash: text('request_hash').notNull(),
  clientId: integer('client_id').notNull(),
  deviceId: integer('device_id').notNull(),
  createdAt,
})

/**
 * Почасовой трафик устройства, байты; hour - unix-секунды начала часа (UTC).
 * Сутки старше двух недель сборщик сворачивает в одну запись, старше года удаляет.
 */
export const deviceTrafficHourly = sqliteTable('device_traffic_hourly', {
  deviceId: integer('device_id').notNull().references(() => devices.id, { onDelete: 'cascade' }),
  hour: integer('hour').notNull(),
  rx: integer('rx').notNull().default(0),
  tx: integer('tx').notNull().default(0),
}, table => [
  primaryKey({ columns: [table.deviceId, table.hour] }),
  index('device_traffic_hourly_hour_idx').on(table.hour),
])

/** Состояние, переживающее рестарт панели: name → JSON (счётчики IKEv2, карта адресов экзитов, версии control). */
export const collectorState = sqliteTable('collector_state', {
  name: text('name').primaryKey(),
  value: text('value').notNull(),
})

/** Нет каскада: история и реквизиты отзыва переживают удаление цели. Payload не отдаётся API. */
export const accessOperations = sqliteTable('access_operations', {
  id: text('id').primaryKey(),
  clientId: integer('client_id').notNull(),
  deviceId: integer('device_id'),
  kind: text('kind').$type<AccessOperation['kind']>().notNull(),
  version: integer('version').notNull(),
  idempotencyKey: text('idempotency_key').notNull().unique(),
  requestHash: text('request_hash').notNull(),
  completionRecorded: integer('completion_recorded', { mode: 'boolean' }).notNull().default(false),
  status: text('status').$type<AccessOperation['status']>().notNull(),
  protocols: text('protocols', { mode: 'json' }).$type<AccessOperation['protocols']>().notNull(),
  payload: text('payload').notNull(),
  actor: text('actor').notNull(),
  attempts: integer('attempts').notNull().default(0),
  nextRetry: integer('next_retry').notNull(),
  createdAt: integer('created_at').notNull(),
}, table => [
  uniqueIndex('access_operations_version').on(table.clientId, table.version),
  index('access_operations_due').on(table.status, table.nextRetry),
])

/** Постоянная очередь панели → бот; payload очищается после завершения. */
export const botOutbox = sqliteTable('bot_outbox', {
  id: text('id').primaryKey(),
  label: text('label').notNull(),
  payload: text('payload').notNull(),
  status: text('status', { enum: ['queued', 'accepted', 'failed'] }).notNull(),
  attempts: integer('attempts').notNull().default(0),
  lastFailure: integer('last_failure').notNull().default(0),
  due: integer('due').notNull(),
  expires: integer('expires').notNull(),
  created: integer('created').notNull(),
}, table => [index('bot_outbox_due_idx').on(table.status, table.due)])

export const routes = sqliteTable('routes', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  type: text('type', { enum: ['domain', 'ip_cidr'] }).notNull(),
  value: text('value').notNull().unique(),
  outbound: text('outbound').notNull(),
  createdAt,
})

/** Журнал событий ленты Обзора, хранится 30 дней. key защищает от повтора. */
export const events = sqliteTable('events', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  at: integer('at').notNull(),
  type: text('type').notNull(),
  level: text('level', { enum: ['alarm', 'warn', 'info', 'ok'] }).notNull(),
  object: text('object'),
  actor: text('actor'),
  title: text('title').notNull(),
  detail: text('detail').notNull().default(''),
  key: text('key').unique(),
}, table => [index('events_at_idx').on(table.at), index('events_object_idx').on(table.object, table.at)])

export type User = typeof users.$inferSelect
export type Client = typeof clients.$inferSelect
export type Device = typeof devices.$inferSelect
export type Route = typeof routes.$inferSelect
