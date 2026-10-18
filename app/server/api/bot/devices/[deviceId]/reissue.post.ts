import { z } from 'zod'
import { botClientView, clientByChat, requireBotAuth } from '../../../../utils/bot-api'
import { accessRequestKey, enqueueAccess } from '../../../../utils/access-operations'
import { readBodyAs } from '../../../../utils/validate'

const Body = z.object({ chatId: z.number().int() })

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isSafeInteger(deviceId) || deviceId < 1) throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  const body = await readBodyAs(event, Body)
  const client = await clientByChat(body.chatId)
  const { operation } = await enqueueAccess({ clientId: client.id, deviceId, kind: 'reissue', actor: 'bot', key: accessRequestKey(event) })
  setResponseStatus(event, 202)
  return { ...await botClientView(client.id), operation }
})
