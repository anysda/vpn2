import type { EventHandler, RouterMethod } from 'h3'
import clients from '../resources/clients'
import devices from '../resources/devices'
import dns from '../resources/dns'
import ops from '../resources/ops'
import routes from '../resources/routes'
import settings from '../resources/settings'

/** Все маршруты `/api`, кроме better-auth: ключ - `МЕТОД /путь`. Типы ответов для фронта выводит `app/api.d.ts`. */
export const api = { ...clients, ...devices, ...dns, ...ops, ...routes, ...settings }

export default defineNitroPlugin((nitro) => {
  for (const [key, handler] of Object.entries<EventHandler>(api)) {
    const [method, path] = key.split(' ') as [string, string]
    nitro.router.add(`/api${path}`, defineEventHandler(handler), method.toLowerCase() as RouterMethod)
  }
})
