import { requireAuth } from '../../utils/auth'
import { clientList } from '../../utils/client-view'

/** Список клиентов: статус, число девайсов и суммарный трафик по девайсам. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return clientList()
})
