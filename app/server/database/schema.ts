import { sql } from 'drizzle-orm'
import { index, integer, primaryKey, sqliteTable, text, uniqueIndex } from 'drizzle-orm/sqlite-core'

const timestamps = {
  createdAt: integer('created_at', { mode: 'timestamp' })
    .notNull()
    .default(sql`(strftime('%s','now'))`),
  updatedAt: integer('updated_at', { mode: 'timestamp' })
    .notNull()
    .default(sql`(strftime('%s','now'))`),
}

/**
 * Учётки панели. Вход, сессии, 2FA и SSO ведёт better-auth (server/utils/auth.ts):
 * таблица у него называется моделью `user`, поля name/email/emailVerified/image
 * обязательны для библиотеки. Почта у учёток не настоящая (`u<id>@panel.invalid`):
 * по ней better-auth привязывает вход через Authentik к уже выбранной учётке.
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
  // Секрет 2FA, заведённый до better-auth (base32 от 20 байт). Библиотека хранит
  // ключ строкой и такой секрет проверить не может; его проверяет хук в
  // server/utils/auth.ts, пока человек не перевыпустит 2FA. null - 2FA новая или нет.
  legacyTotpSecret: text('totp_secret'),
  // Последний принятый 30-секундный шаг TOTP. Код этого и более раннего шага
  // второй раз не принимается (RFC 6238, 5.2, VPN2-62): better-auth сам повтор
  // кода в окне не гасит.
  totpLastStep: integer('totp_last_step'),
  ...timestamps,
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

/**
 * Клиент = человек. Его устройства — в таблице `devices`.
 * Статус («активен» / «заморожен») НЕ хранится — вычисляется из
 * frozenManual + expiresAt, см. server/utils/client-status.ts.
 */
export const clients = sqliteTable('clients', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  name: text('name').notNull(),
  // Фильтрация трафика (AdGuard DNS) — задаётся при создании, наследуется
  // всеми девайсами клиента.
  filterTraffic: integer('filter_traffic', { mode: 'boolean' }).notNull().default(true),
  // Срок действия. null — бессрочно. По истечении клиент попадает в
  // «заморозку» (не удаляется); продление срока возвращает в «активен».
  expiresAt: integer('expires_at', { mode: 'timestamp' }),
  // Лимит девайсов. null — безлимит.
  deviceLimit: integer('device_limit').default(3),
  // Ручная заморозка. Эффективный статус «заморожен» = frozenManual ИЛИ
  // истёкший срок.
  frozenManual: integer('frozen_manual', { mode: 'boolean' }).notNull().default(false),
  // Пароль доступа клиента — 20 символов [A-Za-z]. Им же клиент привязывает
  // свой Telegram к боту (диплинк ?start=<password>).
  password: text('password').notNull(),
  // Telegram привязанного клиента — для самообслуживания в боте.
  // null — клиент ещё не привязал свой Telegram.
  tgChatId: integer('tg_chat_id'),
  tgUsername: text('tg_username'),
  // Предпочитаемый экзит (тег узла, напр. «ch»). null — авто: трафик идёт по
  // дорожкам. Пока узел выключен или мёртв — через foreign-best (переключает
  // failover-watchdog, см. server/utils/client-prefs.ts).
  preferredExit: text('preferred_exit'),
  ...timestamps,
})

/** Девайс = устройство клиента, со своими ключами WG/OVPN/IKEv2. */
export const devices = sqliteTable('devices', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  clientId: integer('client_id')
    .notNull()
    .references(() => clients.id, { onDelete: 'cascade' }),
  name: text('name').notNull(),
  wgPrivateKey: text('wg_private_key'),
  wgPublicKey: text('wg_public_key'),
  wgPresharedKey: text('wg_preshared_key'),
  wgIp: text('wg_ip'),
  ovpnCert: text('ovpn_cert'),
  ovpnKey: text('ovpn_key'),
  // Постоянный адрес OpenVPN: ccd устройства пушит его через ifconfig-push,
  // по нему роутер узнаёт устройство (предпочитаемый экзит). Выдаёт
  // server/utils/openvpn.ts из 10.67.67.2-239.
  ovpnIp: text('ovpn_ip'),
  // IKEv2 EAP-MSCHAPv2 креды. username — slug «<клиент>-<девайс>» через
  // naming.ts, стабилен на всю жизнь устройства; password — 20 символов
  // [A-Za-z0-9], меняется при reissue; ip — статический /32 из пула
  // 10.68.68.0/24 (.1 — server). См. server/utils/ikev2.ts.
  ikev2Username: text('ikev2_username'),
  ikev2Password: text('ikev2_password'),
  ikev2Ip: text('ikev2_ip'),
  // Накопительный трафик девайса за всё время (WG+OpenVPN+IKEv2), байты.
  // Копит фоновый сборщик (server/utils/traffic-collector.ts).
  rxTotal: integer('rx_total').notNull().default(0),
  txTotal: integer('tx_total').notNull().default(0),
  ...timestamps,
}, table => [
  index('devices_client_idx').on(table.clientId),
  // Адрес туннеля один на устройство: без UNIQUE два параллельных создания
  // выбирали один и тот же свободный адрес (VPN2-50).
  uniqueIndex('devices_wg_ip_unique').on(table.wgIp),
  uniqueIndex('devices_ikev2_ip_unique').on(table.ikev2Ip),
  uniqueIndex('devices_ovpn_ip_unique').on(table.ovpnIp),
  // Логин IKEv2 — id в secrets swanctl: на одинаковом логине пускает только
  // одного. buildIkev2Username выбирает свободный, но без UNIQUE два
  // параллельных создания брали один и тот же (VPN2-70).
  uniqueIndex('devices_ikev2_username_unique').on(table.ikev2Username),
])

/**
 * Почасовой трафик девайса (WG+OpenVPN+IKEv2), байты. Наполняет тот же сборщик,
 * что и devices.rx_total/tx_total (server/utils/traffic-collector.ts); из него
 * считаются трафик клиента за сутки/неделю и ТОП. Час — unix-секунды начала
 * часа (UTC): полночь МСК (UTC+3) совпадает с границей часа. Сутки старше двух
 * недель сборщик сворачивает в одну запись с hour = 00:00 МСК, старше года
 * удаляет.
 */
export const deviceTrafficHourly = sqliteTable('device_traffic_hourly', {
  deviceId: integer('device_id')
    .notNull()
    .references(() => devices.id, { onDelete: 'cascade' }),
  hour: integer('hour').notNull(),
  rx: integer('rx').notNull().default(0),
  tx: integer('tx').notNull().default(0),
}, table => [
  primaryKey({ columns: [table.deviceId, table.hour] }),
  index('device_traffic_hourly_hour_idx').on(table.hour),
])

/**
 * Состояние сборщиков, которое должно пережить рестарт панели: name → JSON.
 * 'ikev2' — последние байт-счётчики живых IKE_SA (server/utils/ikev2-counters.ts).
 * 'client-exit-hosts' — последняя карта тег → адрес для переноса выбора клиентов.
 */
export const collectorState = sqliteTable('collector_state', {
  name: text('name').primaryKey(),
  value: text('value').notNull(),
})

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
  createdAt: integer('created_at', { mode: 'timestamp' })
    .notNull()
    .default(sql`(strftime('%s','now'))`),
})

/**
 * Журнал событий для ленты на Обзоре (server/utils/events.ts), хранится 30 дней.
 * level: alarm — тревога, warn — предупреждение, info — запись, ok — восстановление.
 * key — защита от повтора (напоминание об одном сроке уходит один раз).
 */
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

export type User =typeof users.$inferSelect
export type NewUser = typeof users.$inferInsert
export type Client = typeof clients.$inferSelect
export type NewClient = typeof clients.$inferInsert
export type Device = typeof devices.$inferSelect
export type NewDevice = typeof devices.$inferInsert
export type Route = typeof routes.$inferSelect
export type NewRoute = typeof routes.$inferInsert
