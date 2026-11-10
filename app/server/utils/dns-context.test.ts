import { describe, expect, it } from 'vitest'
import { citesGone, findList, listNames, listsView } from './dns-context'
import type { AghFilter, AghFilteringStatus } from './dns-context'
import type { CheckHost } from './agh-parse'

const f = (id: number, over: Partial<AghFilter> = {}): AghFilter => ({ id, name: `L${id}`, url: `https://x/${id}.txt`, enabled: true, rules_count: id * 10, ...over })
const st = (over: Partial<AghFilteringStatus> = {}): AghFilteringStatus => ({ enabled: true, interval: 24, filters: [], whitelist_filters: [], user_rules: [], ...over })

describe('listNames', () => {
  it('null и пустые списки - пустая карта', () => {
    expect(listNames(st({ filters: null, whitelist_filters: null })).size).toBe(0)
    expect(listNames(st()).size).toBe(0)
  })

  it('чёрные и белые вместе, id → название', () => {
    expect([...listNames(st({ filters: [f(1), f(2)], whitelist_filters: [f(3)] }))]).toEqual([[1, 'L1'], [2, 'L2'], [3, 'L3']])
  })

  it('один список; при совпадении id побеждает белый (идёт позже)', () => {
    expect([...listNames(st({ filters: [f(1)], whitelist_filters: null }))]).toEqual([[1, 'L1']])
    expect(listNames(st({ filters: [f(1, { name: 'black' })], whitelist_filters: [f(1, { name: 'white' })] })).get(1)).toBe('white')
  })
})

describe('listsView', () => {
  it('пустой ответ AdGuard (null) - пустые массивы, интервал сохраняется', () => {
    expect(listsView(st({ filters: null, whitelist_filters: null, interval: 0 }))).toEqual({ interval: 0, black: [], white: [] })
  })

  it('поля переименованы, last_updated нет - null', () => {
    const v = listsView(st({
      interval: 72,
      filters: [f(1, { last_updated: '2026-01-01T00:00:00Z' }), f(2, { enabled: false })],
      whitelist_filters: [f(5)],
    }))
    expect(v.interval).toBe(72)
    expect(v.black).toEqual([
      { name: 'L1', url: 'https://x/1.txt', enabled: true, rules: 10, updated: '2026-01-01T00:00:00Z' },
      { name: 'L2', url: 'https://x/2.txt', enabled: false, rules: 20, updated: null },
    ])
    expect(v.white).toEqual([{ name: 'L5', url: 'https://x/5.txt', enabled: true, rules: 50, updated: null }])
  })

  it('порядок списков сохраняется, rules_count 0 остаётся 0', () => {
    const v = listsView(st({ filters: [f(9, { rules_count: 0 }), f(1)] }))
    expect(v.black.map(r => r.name)).toEqual(['L9', 'L1'])
    expect(v.black[0]!.rules).toBe(0)
  })
})

describe('findList', () => {
  const s = st({ filters: [f(1), f(2)], whitelist_filters: [f(3), f(4, { url: 'https://x/1.txt' })] })

  it('ищет в чёрных или в белых в зависимости от флага', () => {
    expect(findList(s, 'https://x/2.txt', false)?.id).toBe(2)
    expect(findList(s, 'https://x/2.txt', true)).toBeUndefined()
    expect(findList(s, 'https://x/3.txt', true)?.id).toBe(3)
    expect(findList(s, 'https://x/3.txt', false)).toBeUndefined()
  })

  it('один и тот же адрес в обоих наборах - каждый находится в своём', () => {
    expect(findList(s, 'https://x/1.txt', false)?.id).toBe(1)
    expect(findList(s, 'https://x/1.txt', true)?.id).toBe(4)
  })

  it('null-наборы и пустой url - undefined; адрес сравнивается точно, с учётом регистра', () => {
    expect(findList(st({ filters: null, whitelist_filters: null }), 'https://x/1.txt', false)).toBeUndefined()
    expect(findList(s, '', false)).toBeUndefined()
    expect(findList(s, 'HTTPS://x/1.txt', false)).toBeUndefined()
  })
})

describe('citesGone', () => {
  const r = (rules: CheckHost['rules']): CheckHost => ({ reason: 'FilteredBlackList', rules })

  it('своё правило из списка исчезнувших - да', () => {
    expect(citesGone(r([{ text: '||a^', filter_list_id: 0 }]), ['||a^'])).toBe(true)
    expect(citesGone(r([{ text: '||b^', filter_list_id: 0 }, { text: '||a^', filter_list_id: 0 }]), ['||a^'])).toBe(true)
  })

  it('правило из чужого списка с тем же текстом - нет (только filter_list_id 0)', () => {
    expect(citesGone(r([{ text: '||a^', filter_list_id: 3 }]), ['||a^'])).toBe(false)
  })

  it('текст не из списка исчезнувших, пустой список, нет правил - нет', () => {
    expect(citesGone(r([{ text: '||a^', filter_list_id: 0 }]), ['||b^'])).toBe(false)
    expect(citesGone(r([{ text: '||a^', filter_list_id: 0 }]), [])).toBe(false)
    expect(citesGone(r([]), ['||a^'])).toBe(false)
    expect(citesGone(r(null), ['||a^'])).toBe(false)
    expect(citesGone({ reason: 'NotFilteredNotFound' }, ['||a^'])).toBe(false)
  })

  it('сравнение текста точное: пробелы и регистр различаются', () => {
    expect(citesGone(r([{ text: '||A^', filter_list_id: 0 }]), ['||a^'])).toBe(false)
    expect(citesGone(r([{ text: '||a^ ', filter_list_id: 0 }]), ['||a^'])).toBe(false)
  })
})
