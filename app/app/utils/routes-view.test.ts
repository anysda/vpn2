import { describe, expect, it } from 'vitest'
import { countryName, routeColumns, rtc } from './routes-view'

const ob = (name: string, delay: number | null) => ({ name, type: 'x', delay, isWarp: name.endsWith('-warp'), isDirectRu: name === 'direct-ru' })

describe('routeColumns', () => {
  // Эталон — вывод прежнего computed из Routes/Section.vue на том же входе.
  it('собирает колонки как раньше: Россия первой, экзиты по алфавиту', () => {
    const cols = routeColumns([
      ob('hy2-us2-warp', 180),
      ob('hy2-de-direct', 42),
      ob('urltest-auto', 1),
      ob('direct-ru', null),
      ob('hy2-de-warp', null),
      ob('hy2-us2-direct', null),
    ])
    expect(cols).toEqual([
      { tag: 'ru', name: 'Россия', sub: 'RU · entry', delay: 0, cells: [
        { outbound: 'direct-ru', label: 'Напрямую', name: 'Россия', delay: 0, warp: false },
      ] },
      { tag: 'de', name: 'Германия', sub: 'DE', delay: 42, cells: [
        { outbound: 'hy2-de-direct', label: 'Напрямую', name: 'Германия', delay: 42, warp: false },
        { outbound: 'hy2-de-warp', label: 'WARP', name: 'Германия · WARP', delay: null, warp: true },
      ] },
      { tag: 'us2', name: 'Соединенные Штаты', sub: 'US2', delay: null, cells: [
        { outbound: 'hy2-us2-direct', label: 'Напрямую', name: 'Соединенные Штаты', delay: null, warp: false },
        { outbound: 'hy2-us2-warp', label: 'WARP', name: 'Соединенные Штаты · WARP', delay: 180, warp: true },
      ] },
    ])
  })

  it('колонка без прямого выхода остаётся без задержки в шапке', () => {
    expect(routeColumns([ob('hy2-nl-warp', 70)])).toEqual([
      { tag: 'nl', name: 'Нидерланды', sub: 'NL', delay: null, cells: [
        { outbound: 'hy2-nl-warp', label: 'WARP', name: 'Нидерланды · WARP', delay: 70, warp: true },
      ] },
    ])
  })
})

describe('countryName', () => {
  it('тег не из двух букв остаётся как есть', () => {
    expect(countryName('lab')).toBe('LAB')
    expect(countryName('us2')).toBe('Соединенные Штаты')
  })
})

describe('rtc', () => {
  it('красит задержку по порогам 60, 100 и 150 мс', () => {
    expect([null, 0, 59, 60, 99, 100, 149, 150].map(rtc)).toEqual(['', '', '', 'rtv', 'rtv', 'rta', 'rta', 'rtp'])
  })
})
