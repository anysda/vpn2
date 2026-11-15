import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { errText, failedStatus, panelErrorText } from './error-text'

const sharedDir = fileURLToPath(new URL('.', import.meta.url))
const appDir = join(sharedDir, '..')

const readJson = (url: URL) => JSON.parse(readFileSync(fileURLToPath(url), 'utf8'))

const table = readJson(new URL('./error-text.json', import.meta.url))
const readText = (rel: string) => readFileSync(join(appDir, '..', rel), 'utf8')

const panelPairs: Record<string, string> = {
  Unauthorized: 'Сессия завершена, войдите заново',
  unauthorized: 'Неверный секрет бота',
  not_found: 'Не найдено',
  invalid_id: 'Неверный идентификатор',
  invalid_params: 'Неверные параметры запроса',
  invalid_chat_id: 'Неверный идентификатор чата Telegram',
  invalid_password: 'Неверный пароль клиента',
  not_linked: 'Telegram не привязан ни к одному клиенту',
  loopback_only: 'API бота доступен только с этой ноды',
  tgbot_secret_not_configured: 'Не задан секрет Telegram-бота в конфиге',
  wg_public_host_not_configured: 'Не задан публичный адрес WireGuard в конфиге',
  ovpn_public_host_not_configured: 'Не задан публичный адрес OpenVPN в конфиге',
  entry_host_not_configured: 'Не задан публичный адрес сервера в конфиге',
  insert_failed: 'Не удалось сохранить запись',
  offline: 'Панель недоступна, проверьте соединение',
  timeout: 'Панель не ответила вовремя'
}

const panelTriples: [string, string, string][] = [
  ['клиент', 'клиента', 'клиентов'],
  ['адрес', 'адреса', 'адресов'],
  ['час', 'часа', 'часов'],
  ['день', 'дня', 'дней'],
  ['правило', 'правила', 'правил'],
  ['список', 'списка', 'списков'],
  ['сервер', 'сервера', 'серверов'],
  ['устройство', 'устройства', 'устройств'],
  ['уведомление', 'уведомления', 'уведомлений']
]

const hasTriple = (values: unknown[], triple: string[]) =>
  values.some(v => Array.isArray(v) && v.length === 3 && v.every((s, i) => s === triple[i]))

describe('единая таблица текстов ошибок', () => {
  it('JSON - объект с ключами panel, bot, botFallback, plural', () => {
    expect(Object.keys(table).sort()).toEqual(['bot', 'botFallback', 'panel', 'plural'])
  })

  it('panel совпадает с 16 парами панели', () => {
    expect(Object.keys(panelPairs).length).toBe(16)
    expect(table.panel).toEqual(panelPairs)
  })

  it('бот берёт bot, botFallback и plural.botDevice из этой таблицы', () => {
    expect(Object.keys(table.bot).length).toBe(32)
    expect(table.botFallback).toBe('Не удалось выполнить запрос. Попробуйте позже.')
    expect(readText('telegram/Dockerfile')).toContain('COPY --chmod=0644 app/shared/error-text.json ./vpn2bot/error-text.json')
    const texts = readText('telegram/vpn2bot/texts.py')
    for (const use of ["with_name('error-text.json')", "_TABLE['bot']", "_TABLE['botFallback']", '_TABLE["plural"]["botDevice"]']) {
      expect(texts, use).toContain(use)
    }
  })

  it('plural содержит тройки панели и бота', () => {
    expect(table.plural).toEqual({
      client: ['клиент', 'клиента', 'клиентов'],
      address: ['адрес', 'адреса', 'адресов'],
      hour: ['час', 'часа', 'часов'],
      day: ['день', 'дня', 'дней'],
      rule: ['правило', 'правила', 'правил'],
      list: ['список', 'списка', 'списков'],
      server: ['сервер', 'сервера', 'серверов'],
      device: ['устройство', 'устройства', 'устройств'],
      notification: ['уведомление', 'уведомления', 'уведомлений'],
      botDevice: ['девайс', 'девайса', 'девайсов'],
    })
    const values = Object.values(table.plural)
    for (const triple of panelTriples) {
      expect(hasTriple(values, triple), `нет тройки панели ${triple.join(' / ')}`).toBe(true)
    }
  })

  it('panelErrorText отдаёт текст по коду и undefined по чужому', () => {
    for (const [code, text] of Object.entries(panelPairs)) {
      expect(panelErrorText(code)).toBe(text)
    }
    expect(panelErrorText('no_such_code')).toBeUndefined()
    expect(panelErrorText('')).toBeUndefined()
    expect(panelErrorText('toString')).toBeUndefined()
  })

  it('текстов панели нет литералами в коде', () => {
    const files: string[] = []
    for (const dir of ['server', 'app', 'shared']) {
      const root = join(appDir, dir)
      for (const rel of readdirSync(root, { recursive: true, encoding: 'utf8' })) {
        if (rel.split(/[\\/]/).includes('node_modules')) continue
        if (!/\.(ts|vue)$/.test(rel) || rel.endsWith('.test.ts')) continue
        files.push(join(root, rel))
      }
    }
    expect(files.length).toBeGreaterThan(100)

    const found: string[] = []
    for (const file of files) {
      const source = readFileSync(file, 'utf8')
      for (const text of Object.values(panelPairs)) {
        for (const q of ['\'', '"', '`']) {
          if (source.includes(q + text + q)) {
            found.push(`${file.slice(appDir.length + 1)}: ${text}`)
          }
        }
      }
    }
    expect(found, `литералы текстов: ${found.join('; ')}`).toEqual([])
  })
})

describe('errText: текст ошибки запроса для человека', () => {
  const fetchError = (status?: number, data?: unknown, message = '[GET] "/api/clients/1": <no response> Failed to fetch') =>
    Object.assign(new Error(message), { name: 'FetchError', data, response: status ? { status } : undefined, statusMessage: undefined })
  const nuxtError = (cause: Error) => Object.assign(new Error('x'), { name: 'NuxtError', statusCode: 500, cause })

  it('без ответа - панель недоступна, без [GET] и английского', () => {
    expect(errText(fetchError())).toBe('Панель недоступна, проверьте соединение')
    expect(errText(nuxtError(fetchError()))).toBe('Панель недоступна, проверьте соединение')
  })
  it('таймаут useFetch (NuxtError с TimeoutError) - панель не ответила вовремя', () => {
    const timeout = Object.assign(new Error('signal timed out'), { name: 'TimeoutError' })
    expect(errText(Object.assign(new Error('signal timed out'), { name: 'NuxtError', statusCode: 500, cause: timeout }))).toBe('Панель не ответила вовремя')
  })
  it('таймаут - панель не ответила вовремя', () => {
    expect(errText(fetchError(undefined, undefined, '[GET] "/api/x": <no response> The operation was aborted due to timeout'))).toBe('Панель не ответила вовремя')
  })
  it('обрыв связи в useFetch (NuxtError с TypeError внутри) - тоже про недоступность', () => {
    const wrapped = Object.assign(new Error('[GET] "/api/clients": <no response> Failed to fetch'), { name: 'NuxtError', cause: new TypeError('Failed to fetch') })
    expect(errText(wrapped)).toBe('Панель недоступна, проверьте соединение')
    expect(failedStatus(wrapped)).toBeUndefined()
  })
  it('401: закрытая ручка - текст конца сессии, вход - свой текст', () => {
    expect(errText(fetchError(401, { message: 'Сессия завершена, войдите заново' }))).toBe('Сессия завершена, войдите заново')
    expect(errText(fetchError(401, { message: 'Неверный текущий пароль' }))).toBe('Неверный текущий пароль')
    expect(failedStatus(nuxtError(fetchError(401)))).toBe(401)
  })
  it('русский текст сервера - как есть, английский - запасной текст', () => {
    expect(errText(fetchError(400, { message: 'Проверьте поле «Имя»' }))).toBe('Проверьте поле «Имя»')
    expect(errText(fetchError(500, { message: 'Internal Server Error' }), 'Ошибка сохранения')).toBe('Ошибка сохранения')
  })
  it('своя ошибка страницы - свой текст', () => {
    expect(errText(new Error('Дата — в формате ДД.ММ.ГГГГ'))).toBe('Дата — в формате ДД.ММ.ГГГГ')
  })
})
