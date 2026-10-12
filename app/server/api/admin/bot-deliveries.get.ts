import { useAuth } from '../../utils/auth'
import { botDeliveries } from '../../utils/bot-events'

export default defineEventHandler(async (event) => {
  setHeader(event, 'Cache-Control', 'no-store')
  // Фоновый опрос может пересечься с выходом или сменой сессии при 2FA.
  // Как get-session, возвращаем пустой результат; историю без сессии не выдаём.
  if (!await useAuth().api.getSession({ headers: event.headers })) return []
  return botDeliveries()
})
