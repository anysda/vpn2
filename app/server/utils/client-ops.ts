import { and, count, eq, ne, notExists, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import type { Client } from '../database/schema'
import { clients, devices } from '../database/schema'
import { notifyBot, notifyClient } from './bot-events'
import { minExpiryMs } from './expiry'
import { generateClientPassword } from './password'
import { syncWireguardConfig } from './wireguard'
import { killOvpnClientQuiet, ovpnCn, removeCcd, syncOpenvpnConfig, withRevokedDeviceCerts } from './openvpn'
import { syncIkev2, terminateIkev2Sa } from './ikev2'
import { syncClientPrefsFile } from './client-prefs'
import { assertKnownOutbound } from './route-outbound'
import { joinDetail, recordEvent } from './events'
import { pluralRu } from '../../shared/plural'

/**
 * Общие операции над клиентами — используются и панелью (session-auth),
 * и ботом (Bearer-auth админский чат). Здесь же — валидация и уведомления
 * привязанному клиенту в Telegram.
 */

export interface ClientPatch {
  name?: string
  expiresAt?: string | null
  deviceLimit?: number | null
  frozenManual?: boolean
  // тег узла экзита или null — «авто»
  preferredExit?: string | null
}

export interface ClientCreateInput {
  name: string
  filterTraffic?: boolean
  expiresAt?: string | null
  deviceLimit?: number | null
}

const devicesText = (n: number) => `${n} ${pluralRu(n, ['устройство', 'устройства', 'устройств'])}`

/** Срок для журнала — дата по Москве, как её видит админ. */
function termText(expiresAt: Date | null): string {
  return expiresAt ? `до ${expiresAt.toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' })}` : 'бессрочно'
}

/** Срок действия нельзя ставить раньше завтрашней даты. */
function assertExpiryAllowed(expiresAt: string | null | undefined): void {
  if (expiresAt && new Date(expiresAt).getTime() < minExpiryMs()) {
    throw createError({
      statusCode: 422,
      statusMessage: 'invalid_expiry', message: 'Срок действия не может быть раньше завтрашней даты',
    })
  }
}

/** Создать клиента (имя + опции). Пароль генерится автоматически. */
export async function createClient(input: ClientCreateInput, actor: string): Promise<Client> {
  const db = useDb()
  assertExpiryAllowed(input.expiresAt)

  const name = input.name.trim()
  const dup = await db.select({ id: clients.id }).from(clients).where(eq(clients.name, name)).limit(1)
  if (dup.length > 0) {
    throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${name}» уже существует` })
  }

  const values: typeof clients.$inferInsert = {
    name,
    filterTraffic: input.filterTraffic ?? true,
    password: generateClientPassword(),
  }
  if (input.expiresAt !== undefined) values.expiresAt = input.expiresAt ? new Date(input.expiresAt) : null
  if (input.deviceLimit !== undefined) values.deviceLimit = input.deviceLimit

  const [row] = await db.insert(clients).values(values).returning()
  if (!row) throw createError({ statusCode: 500, statusMessage: 'insert_failed' })
  await notifyBot('client_created', { name: row.name })
  await recordEvent({ type: 'client_created', level: 'info', object: `client:${row.id}`, actor, title: `Клиент создан · ${row.name}`, detail: actor })
  return row
}

/** Обновить клиента: валидация, апдейт, ресинк WG/OVPN, уведомления клиенту. */
export async function updateClient(id: number, patch: ClientPatch, actor: string): Promise<Client> {
  const db = useDb()
  assertExpiryAllowed(patch.expiresAt)

  const [before] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Лимит девайсов нельзя опустить ниже текущего числа устройств клиента.
  if (typeof patch.deviceLimit === 'number') {
    const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))
    const have = Number(dc?.n ?? 0)
    if (patch.deviceLimit < have) {
      throw createError({
        statusCode: 409,
        statusMessage: 'device_limit_below_count', message: `У клиента уже ${have} ${pluralRu(have, ['устройство', 'устройства', 'устройств'])} — лимит не может быть меньше`,
      })
    }
  }

  const upd: Partial<typeof clients.$inferInsert> = { updatedAt: new Date() }
  if (patch.name !== undefined) {
    const name = patch.name.trim()
    const dup = await db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.name, name), ne(clients.id, id)))
      .limit(1)
    if (dup.length > 0) {
      throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${name}» уже существует` })
    }
    upd.name = name
  }
  if (patch.expiresAt !== undefined) upd.expiresAt = patch.expiresAt ? new Date(patch.expiresAt) : null
  if (patch.deviceLimit !== undefined) upd.deviceLimit = patch.deviceLimit
  if (patch.frozenManual !== undefined) upd.frozenManual = patch.frozenManual
  if (patch.preferredExit !== undefined) {
    if (patch.preferredExit) await assertKnownOutbound(`hy2-${patch.preferredExit}-direct`)
    upd.preferredExit = patch.preferredExit || null
  }

  // Проверка sing-box выше уступает управление другим запросам. Имя могло
  // стать занятым, а число устройств вырасти: проверяем ограничения в том
  // же SQL, который записывает изменения, без отдельной write-транзакции.
  const conditions = [eq(clients.id, id)]
  if (upd.name !== undefined) {
    conditions.push(notExists(db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.name, upd.name), ne(clients.id, id)))))
  }
  if (typeof patch.deviceLimit === 'number') {
    conditions.push(sql`(select count(*) from ${devices} where ${devices.clientId} = ${id}) <= ${patch.deviceLimit}`)
  }
  const [row] = await db.update(clients).set(upd).where(and(...conditions)).returning()
  if (!row) {
    const [exists] = await db.select({ id: clients.id }).from(clients).where(eq(clients.id, id)).limit(1)
    if (!exists) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    if (upd.name !== undefined) {
      const dup = await db.select({ id: clients.id }).from(clients)
        .where(and(eq(clients.name, upd.name), ne(clients.id, id))).limit(1)
      if (dup.length) {
        throw createError({ statusCode: 409, statusMessage: 'client_exists', message: `Клиент с именем «${upd.name}» уже существует` })
      }
    }
    throw createError({
      statusCode: 409,
      statusMessage: 'device_limit_below_count', message: 'Лимит не может быть меньше числа устройств клиента',
    })
  }

  // Смена заморозки/срока меняет активность — пере-синхроним девайсы.
  if (patch.frozenManual !== undefined || patch.expiresAt !== undefined) {
    await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after client update failed'))
    await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after client update failed'))
    // IKEv2: при заморозке клиента терминируем все его активные SA — иначе
    // charon держит туннель до rekey. syncIkev2 затем выпиливает secrets.
    if (patch.frozenManual === true) {
      const devs = await db.select({ username: devices.ikev2Username })
        .from(devices).where(eq(devices.clientId, id))
      for (const d of devs) {
        if (!d.username) continue
        await terminateIkev2Sa(d.username).catch(err =>
          useLogger().warn({ err: (err as Error).message }, 'ikev2 terminate after freeze skipped'))
      }
    }
    await syncIkev2().catch(err => useLogger().error({ err }, 'ikev2 sync after client update failed'))
  }

  if (patch.preferredExit !== undefined) {
    await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))
  }

  const object = `client:${row.id}`
  if (row.frozenManual !== before.frozenManual) {
    const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))
    await recordEvent({
      type: row.frozenManual ? 'client_frozen' : 'client_unfrozen', level: 'info', object, actor,
      title: `${row.frozenManual ? 'Клиент заморожен' : 'Клиент разморожен'} · ${row.name}`,
      detail: joinDetail(devicesText(Number(dc?.n ?? 0)), 'вручную', actor),
    })
  }
  if ((before.expiresAt?.getTime() ?? null) !== (row.expiresAt?.getTime() ?? null)) {
    await recordEvent({ type: 'client_term', level: 'info', object, actor, title: `Срок изменён · ${row.name}`, detail: joinDetail(termText(row.expiresAt), actor) })
  }

  // Уведомления привязанному клиенту об изменениях аккаунта.
  const chat = before.tgChatId
  if (chat) {
    if (patch.name !== undefined && row.name !== before.name) {
      await notifyClient(chat, `✏️ Имя вашего профиля изменено на «${row.name}».`, row.name)
    }
    if (patch.deviceLimit !== undefined) {
      const rank = (v: number | null) => (v === null ? Infinity : v)
      const oldR = rank(before.deviceLimit)
      const newR = rank(row.deviceLimit)
      if (newR > oldR) {
        await notifyClient(chat, row.deviceLimit === null
          ? '📈 Лимит устройств снят — теперь без ограничений.'
          : `📈 Ваш лимит устройств повышен до ${row.deviceLimit}.`, row.name)
      }
      else if (newR < oldR) {
        await notifyClient(chat, `📉 Ваш лимит устройств понижен до ${row.deviceLimit}.`, row.name)
      }
    }
    if (patch.frozenManual !== undefined && patch.frozenManual !== before.frozenManual) {
      await notifyClient(chat, patch.frozenManual
        ? '❄️ Ваш доступ к VPN приостановлен администратором.'
        : '✅ Ваш доступ к VPN восстановлен.', row.name)
    }
    if (patch.expiresAt !== undefined) {
      const oldT = before.expiresAt ? before.expiresAt.getTime() : null
      const newT = row.expiresAt ? row.expiresAt.getTime() : null
      if (oldT !== newT) {
        await notifyClient(chat, row.expiresAt
          ? `🗓 Срок действия вашего ключа: до ${row.expiresAt.toLocaleDateString('ru-RU')}.`
          : '🗓 Срок действия ключа снят — доступ бессрочный.', row.name)
      }
    }
  }

  return row
}

/** Удалить клиента: каскадно девайсы, ресинк WG, отзыв OVPN-сертификатов. */
export async function deleteClient(id: number, actor: string): Promise<void> {
  const db = useDb()
  // При частичном отзыве оставляем ВСЕ записи: повтор проходит уже отозванные
  // серийники и доводит оставшиеся до CRL, прежде чем сработает каскад DELETE.
  const { removed, devRows } = await withRevokedDeviceCerts(eq(devices.clientId, id), async (devRows) => {
    const [removed] = await db.delete(clients).where(eq(clients.id, id)).returning()
    return { removed, devRows }
  })
  if (!removed) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after client delete failed'))

  for (const d of devRows) {
    await removeCcd(ovpnCn(d.id)).catch(() => {})
    await killOvpnClientQuiet(ovpnCn(d.id))
  }

  // IKEv2: терминируем SA каждого устройства + syncIkev2 убирает secrets.
  for (const d of devRows) {
    if (!d.ikev2Username) continue
    await terminateIkev2Sa(d.ikev2Username).catch(err =>
      useLogger().warn({ err: (err as Error).message }, 'ikev2 terminate after client delete skipped'))
  }
  await syncIkev2().catch(err => useLogger().error({ err }, 'ikev2 sync after client delete failed'))
  await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))

  await recordEvent({
    type: 'client_deleted', level: 'info', object: `client:${removed.id}`, actor,
    title: `Клиент удалён · ${removed.name}`, detail: joinDetail(devicesText(devRows.length), actor),
  })

  // Даже переполненная очередь не должна помешать отзыву доступа выше.
  // Ждём постоянной записи события до завершения запроса.
  if (removed.tgChatId) {
    await notifyClient(removed.tgChatId, '🗑 Ваш профиль удалён администратором. Доступ к VPN прекращён.', removed.name)
  }
}

/** Общее удаление устройства из панели и бота; ошибка CA оставляет запись для повтора. */
export async function deleteDevice(clientId: number, deviceId: number) {
  const db = useDb()
  const device = await withRevokedDeviceCerts(and(eq(devices.id, deviceId), eq(devices.clientId, clientId))!, async ([device]) => {
    if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    await db.delete(devices).where(eq(devices.id, deviceId))
    return device
  })
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device delete failed'))
  await removeCcd(ovpnCn(device.id)).catch(() => {})
  await killOvpnClientQuiet(ovpnCn(device.id))
  if (device.ikev2Username) {
    await terminateIkev2Sa(device.ikev2Username).catch(err =>
      useLogger().warn({ err: (err as Error).message }, 'ikev2 terminate after device delete skipped'))
  }
  await syncIkev2().catch(err => useLogger().error({ err }, 'ikev2 sync after device delete failed'))
  await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))
  return device
}
