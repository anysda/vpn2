import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { panelErrorText } from './error-text'

const sharedDir = fileURLToPath(new URL('.', import.meta.url))
const appDir = join(sharedDir, '..')

const readJson = (url: URL) => JSON.parse(readFileSync(fileURLToPath(url), 'utf8'))

const table = readJson(new URL('./error-text.json', import.meta.url))
const readText = (rel: string) => readFileSync(join(appDir, '..', rel), 'utf8')

const panelPairs: Record<string, string> = {
  Unauthorized: 'Нужно войти',
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
  insert_failed: 'Не удалось сохранить запись'
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

  it('panel совпадает с 14 парами панели', () => {
    expect(Object.keys(panelPairs).length).toBe(14)
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
