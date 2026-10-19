import { z } from 'zod'
import { requireAuth } from '../../../utils/auth'
import { actorOf, joinDetail, recordEvent } from '../../../utils/events'
import { toggleExit } from '../../../utils/exit-control'
import { nodeInstances } from '../../../utils/vm-client'

const Body = z.object({ disabled: z.boolean() })

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const tag = String(getRouterParam(event, 'tag') || '')
  const exits = nodeInstances().map(n => n.tag).filter(t => t !== 'ru')
  if (!exits.includes(tag)) {
    throw createError({ statusCode: 404, statusMessage: 'unknown_exit' })
  }

  const body = await readValidatedBody(event, Body.parse)
  // Хотя бы один экзит должен остаться включённым — иначе watchdog'у некуда
  // уводить иностранный трафик.
  // Прежнее состояние и запись журнала - внутри очереди переключений: иначе
  // одновременный PATCH успевает записать своё между чтением и записью.
  const written = await toggleExit(tag, body.disabled, exits, async (wasDisabled) => {
    useLogger().info({ tag, wasDisabled, disabled: body.disabled }, 'exit toggled manually')
    if (wasDisabled === body.disabled) return
    await recordEvent({
      type: body.disabled ? 'exit_disabled' : 'exit_enabled', level: 'info', object: `node:${tag}`, actor: actorOf(user),
      title: `${body.disabled ? 'Экзит выключен' : 'Экзит включён'} · ${tag.toUpperCase()}`, detail: joinDetail('вручную', actorOf(user)),
    })
  })
  if (!written) {
    throw createError({ statusCode: 409, statusMessage: 'last_enabled_exit' })
  }
  return { tag, disabled: body.disabled }
})
