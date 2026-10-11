import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { normalizeScreenLayout, SCREEN_MODULE_IDS } from '#shared/screen-layout'
import { useDb } from '../../database/client'
import { users } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { readBodyAs } from '../../utils/validate'

const Column = z.array(z.enum(SCREEN_MODULE_IDS))
const Body = z.object({ left: Column, right: Column, hidden: Column })
  .refine(b => new Set([...b.left, ...b.right, ...b.hidden]).size === b.left.length + b.right.length + b.hidden.length)

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const layout = normalizeScreenLayout(await readBodyAs(event, Body))
  await useDb()
    .update(users)
    .set({ screenLayout: layout, updatedAt: new Date() })
    .where(eq(users.id, user.id))
  return layout
})
