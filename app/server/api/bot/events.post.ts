import { z } from 'zod'
import { requireBotAuth } from '../../utils/bot-api'
import { recordEvent } from '../../utils/events'
import { readBodyAs } from '../../utils/validate'

// Тревоги, о которых панель узнаёт только снаружи: от бота (Telegram отверг
// сообщение) и от скрипта бэкапа на этой же ноде. Только перечисленные типы.
const Body = z.discriminatedUnion('type', [
  z.object({ type: z.literal('telegram_rejected'), status: z.number().int().min(100).max(599), recipient: z.enum(['admin', 'client']) }),
  z.object({ type: z.literal('backup_failed'), detail: z.string().trim().max(200).default('') }),
])

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readBodyAs(event, Body)
  if (body.type === 'telegram_rejected') {
    await recordEvent({
      type: 'telegram_rejected', level: 'alarm', object: 'bot', title: 'Telegram не принял сообщение бота',
      detail: `${body.recipient === 'client' ? 'клиенту' : 'админу'} · код ${body.status}`,
    })
  }
  else {
    await recordEvent({ type: 'backup_failed', level: 'alarm', object: 'backup', title: 'Бэкап не удался', detail: body.detail })
  }
  return { ok: true }
})
