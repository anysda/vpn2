import { sql } from 'drizzle-orm'
import { index, integer, sqliteTable, text } from 'drizzle-orm/sqlite-core'

const timestamps = {
  createdAt: integer('created_at', { mode: 'timestamp' })
    .notNull()
    .default(sql`(strftime('%s','now'))`),
  updatedAt: integer('updated_at', { mode: 'timestamp' })
    .notNull()
    .default(sql`(strftime('%s','now'))`),
}

export const users = sqliteTable('users', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  username: text('username').notNull().unique(),
  passwordHash: text('password_hash').notNull(),
  totpSecret: text('totp_secret'),
  ...timestamps,
})

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
])

export const routes = sqliteTable('routes', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  type: text('type', { enum: ['domain', 'ip_cidr'] }).notNull(),
  value: text('value').notNull().unique(),
  outbound: text('outbound').notNull(),
  createdAt: integer('created_at', { mode: 'timestamp' })
    .notNull()
    .default(sql`(strftime('%s','now'))`),
})

export type User = typeof users.$inferSelect
export type NewUser = typeof users.$inferInsert
export type Client = typeof clients.$inferSelect
export type NewClient = typeof clients.$inferInsert
export type Device = typeof devices.$inferSelect
export type NewDevice = typeof devices.$inferInsert
export type Route = typeof routes.$inferSelect
export type NewRoute = typeof routes.$inferInsert
