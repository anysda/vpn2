import { requireAuth } from '../../utils/auth'
import { filteringStatus } from '../../utils/dns-context'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return { text: ((await filteringStatus()).user_rules ?? []).join('\n') }
})
