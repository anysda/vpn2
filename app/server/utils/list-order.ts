import { asc, desc } from 'drizzle-orm'
import { clients, devices } from '../database/schema'

// created_at хранится до секунды: второй ключ id делает порядок равных строк постоянным.
export const clientsNewestFirst = [desc(clients.createdAt), desc(clients.id)]
export const devicesOldestFirst = [asc(devices.createdAt), asc(devices.id)]
