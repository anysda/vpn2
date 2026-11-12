import { pino, type Logger } from 'pino'

let logger: Logger | null = null

export function useLogger() {
  logger ??= pino({ level: process.env.LOG_LEVEL ?? 'info' })
  return logger
}
