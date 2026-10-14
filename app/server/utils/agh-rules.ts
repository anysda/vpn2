import { isIP } from 'node:net'
import type { CheckHost } from './agh-parse'

// Свои правила AdGuard Home и вердикт проверки фильтрации.
// AdGuard принимает любой текст: строку с ошибкой он молча пропускает, и
// человек думает, что правило работает. Поэтому разбор — здесь, до отправки.

export interface RuleError { line: number, text: string, why: string }

// Модификаторы DNS-правил AdGuard Home; у части без значения смысла нет.
const MODIFIERS = new Set(['important', 'client', 'ctag', 'dnstype', 'dnsrewrite', 'denyallow', 'badfilter'])
const NEED_VALUE = new Set(['client', 'ctag', 'dnstype', 'denyallow'])

/** Модификаторы через запятую; запятая в кавычках — часть значения. */
function splitModifiers(s: string): string[] {
  const out: string[] = []
  let cur = ''
  let q = ''
  for (const ch of s) {
    if (q) {
      if (ch === q) q = ''
    }
    else if (ch === '\'' || ch === '"') q = ch
    else if (ch === ',') {
      out.push(cur)
      cur = ''
      continue
    }
    cur += ch
  }
  out.push(cur)
  return out
}

function checkRule(raw: string): string | null {
  const r = raw.trim()
  if (!r || r.startsWith('!') || r.startsWith('#')) return null
  const hosts = r.split(/\s+/)
  if (hosts.length > 1 && isIP(hosts[0]!)) return null
  let body = r.startsWith('@@') ? r.slice(2) : r
  let mods: string | null = null
  if (body.startsWith('/')) {
    const end = body.lastIndexOf('/')
    if (end <= 0) return 'не закрыто регулярное выражение — нужен / в конце'
    try {
      new RegExp(body.slice(1, end))
    }
    catch {
      return 'регулярное выражение с ошибкой'
    }
    const tail = body.slice(end + 1)
    if (tail && !tail.startsWith('$')) return 'после регулярного выражения ждётся $ или конец строки'
    if (tail) mods = tail.slice(1)
  }
  else {
    const at = body.indexOf('$')
    if (at >= 0) {
      mods = body.slice(at + 1)
      body = body.slice(0, at)
    }
    if (/\s/.test(body)) return 'пробел внутри правила'
    if (!body.replace(/^\|\|?/, '').replace(/[\^|]+$/, '')) return 'нет адреса'
  }
  if (mods === null) return null
  // `||x^$` AdGuard принимает и применяет как `||x^`.
  if (!mods.trim()) return null
  for (const m of splitModifiers(mods)) {
    const [name = '', ...rest] = m.trim().replace(/^~/, '').split('=')
    if (!MODIFIERS.has(name)) return `неизвестный модификатор $${name}`
    const value = rest.join('=').trim()
    if (NEED_VALUE.has(name) && !value.replace(/^(['"])(.*)\1$/, '$2').trim()) return `у $${name} нет значения`
  }
  return null
}

/** Строки, которые AdGuard молча пропустит. Номера строк — с единицы. */
export function checkRules(lines: string[]): RuleError[] {
  const errors: RuleError[] = []
  lines.forEach((text, i) => {
    const why = checkRule(text)
    if (why) errors.push({ line: i + 1, text: text.trim(), why })
  })
  return errors
}

export interface Verdict {
  kind: 'blk' | 'ok' | 'rw'
  label: string
  /** Пояснение: текст, правило моноширинным, текст. */
  why: [string, string, string]
}

/** Ответ /control/filtering/check_host — словами, с правилом и источником. */
export function verdict(r: CheckHost, lists: Map<number, string>, services: Map<string, string>): Verdict {
  const rule = r.rules?.[0]
  const text = rule?.text ?? ''
  const id = rule?.filter_list_id ?? 0
  const list = lists.get(id) ?? `Список ${id}`
  switch (r.reason) {
    case 'FilteredBlackList':
      return { kind: 'blk', label: 'Заблокирован', why: ['Правило ', text, id === 0 ? ' из своих правил' : ` из списка «${list}»`] }
    case 'NotFilteredWhiteList':
      return { kind: 'ok', label: 'Разрешён', why: id === 0
        ? ['Правило ', text, ' разблокирует его, даже если он есть в списках']
        : ['Правило ', text, ` из белого списка «${list}»`] }
    case 'FilteredBlockedService': {
      const s = r.service_name ?? ''
      return { kind: 'blk', label: 'Заблокирован', why: [`Заблокирован сервис «${services.get(s) ?? s}»`, '', ''] }
    }
    case 'FilteredSafeBrowsing':
      return { kind: 'blk', label: 'Заблокирован', why: ['Опасный сайт: фишинг или вредонос', '', ''] }
    case 'FilteredParental':
      return { kind: 'blk', label: 'Заблокирован', why: ['Сайт для взрослых', '', ''] }
    case 'FilteredSafeSearch':
      return { kind: 'rw', label: 'Перезаписан', why: ['Безопасный поиск', '', ''] }
    case 'Rewrite':
    case 'RewriteEtcHosts':
      return { kind: 'rw', label: 'Перезаписан', why: ['Ответ ', r.ip_addrs?.[0] || r.cname || 'пусто', ' из перезаписей DNS'] }
    case 'RewriteRule':
      return { kind: 'rw', label: 'Перезаписан', why: ['Правило ', text, id === 0 ? ' из своих правил' : ` из списка «${list}»`] }
    default:
      return { kind: 'ok', label: 'Не фильтруется', why: ['Ни одно правило не подошло, запрос уйдёт на серверы выше', '', ''] }
  }
}
