import { z } from 'zod'
import { requireAuth } from '../../../utils/auth'
import { toggleExit } from '../../../utils/exit-control'
import { nodeInstances } from '../../../utils/vm-client'

const Body = z.object({ disabled: z.boolean() })

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const tag = String(getRouterParam(event, 'tag') || '')
  const exits = nodeInstances().map(n => n.tag).filter(t => t !== 'ru')
  if (!exits.includes(tag)) {
    throw createError({ statusCode: 404, statusMessage: 'unknown_exit' })
  }

  const body = await readValidatedBody(event, Body.parse)
  // Хотя бы один экзит должен остаться включённым — иначе watchdog'у некуда
  // уводить иностранный трафик.
  if (!await toggleExit(tag, body.disabled, exits)) {
    throw createError({ statusCode: 409, statusMessage: 'last_enabled_exit' })
  }
  useLogger().info({ tag, disabled: body.disabled }, 'exit toggled manually')
  return { tag, disabled: body.disabled }
})
