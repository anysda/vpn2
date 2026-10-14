import { test, expect } from '@playwright/test'
import { serverModule } from './support/server-module'

// Свои правила AdGuard Home: строки, которые настоящий AdGuard 0.107 принимает
// ответом 200 и молча не применяет, и вердикт /control/filtering/check_host.
type Rules = typeof import('../server/utils/agh-rules')
const r = serverModule<Rules>(new URL('../server/utils/agh-rules.ts', import.meta.url), {})

test('свои правила: строки, которые AdGuard молча пропустит, — ошибка с номером строки', () => {
  const lines = [
    '! реклама и счётчики',
    '||mc.yandex.ru^',
    '||doubleclick.net^$important',
    '||an.yandex.ru^$client=\'Мама, Папа\'',
    '||baz.example^$important,dnstype=A',
    '/^ads\\d+\\.example$/',
    '@@||t.co^',
    '0.0.0.0 hosts.example',
    'good.example',
    '# тоже комментарий',
    '',
    '||foo.example^$bogus',
    '||bar.example^$client=',
    '@@',
    '/unclosed[/',
    '||sp ace.example^',
    '||x.example^$',
  ]
  expect(r.checkRules(lines)).toEqual([
    { line: 12, text: '||foo.example^$bogus', why: 'неизвестный модификатор $bogus' },
    { line: 13, text: '||bar.example^$client=', why: 'у $client нет значения' },
    { line: 14, text: '@@', why: 'нет адреса' },
    { line: 15, text: '/unclosed[/', why: 'регулярное выражение с ошибкой' },
    { line: 16, text: '||sp ace.example^', why: 'пробел внутри правила' },
  ])
  expect(r.checkRules(['/abc'])).toEqual([{ line: 1, text: '/abc', why: 'не закрыто регулярное выражение — нужен / в конце' }])
  expect(r.checkRules(['||', '||^'])).toHaveLength(2)
})

test('проверка фильтрации: вердикт называет правило и откуда оно', () => {
  const lists = new Map([[1, 'AdGuard DNS filter'], [7, 'Белый anysda']])
  const services = new Map([['youtube', 'YouTube']])
  const v = (x: Record<string, unknown>) => r.verdict({ reason: 'NotFilteredNotFound', ...x }, lists, services)
  expect(v({ reason: 'FilteredBlackList', rules: [{ text: '||example.org^', filter_list_id: 0 }] }))
    .toEqual({ kind: 'blk', label: 'Заблокирован', why: ['Правило ', '||example.org^', ' из своих правил'] })
  expect(v({ reason: 'FilteredBlackList', rules: [{ text: '||doubleclick.net^', filter_list_id: 1 }] }))
    .toEqual({ kind: 'blk', label: 'Заблокирован', why: ['Правило ', '||doubleclick.net^', ' из списка «AdGuard DNS filter»'] })
  expect(v({ reason: 'NotFilteredWhiteList', rules: [{ text: '@@||t.co^', filter_list_id: 0 }] }))
    .toEqual({ kind: 'ok', label: 'Разрешён', why: ['Правило ', '@@||t.co^', ' разблокирует его, даже если он есть в списках'] })
  expect(v({ reason: 'NotFilteredWhiteList', rules: [{ text: '@@||a.ru^', filter_list_id: 7 }] }).why[2]).toBe(' из белого списка «Белый anysda»')
  expect(v({ reason: 'Rewrite', ip_addrs: ['192.0.2.1'] }))
    .toEqual({ kind: 'rw', label: 'Перезаписан', why: ['Ответ ', '192.0.2.1', ' из перезаписей DNS'] })
  expect(v({ reason: 'FilteredBlockedService', service_name: 'youtube' }).why[0]).toBe('Заблокирован сервис «YouTube»')
  expect(v({})).toEqual({ kind: 'ok', label: 'Не фильтруется', why: ['Ни одно правило не подошло, запрос уйдёт на серверы выше', '', ''] })
})
