import { pino } from 'pino'

let _logger: pino.Logger | null = null

export function useLogger() {
  if (!_logger) {
    const isDev = process.env.NODE_ENV !== 'production'
    _logger = pino({
      level: process.env.LOG_LEVEL ?? 'info',
      ...(isDev && {
        transport: {
          target: 'pino-pretty',
          options: { colorize: true, translateTime: 'HH:MM:ss' },
        },
      }),
    })
  }
  return _logger
}
