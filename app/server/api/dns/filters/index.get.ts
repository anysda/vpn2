import { requireAuth } from '../../../utils/auth'
import { filteringStatus, listsView } from '../../../utils/dns-context'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return listsView(await filteringStatus())
})
