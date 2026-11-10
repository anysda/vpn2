import { describe, expect, it } from 'vitest'
import { pluralRu } from './plural'
import table from './error-text.json' with { type: 'json' }

const forms: [string, string, string] = ['устройство', 'устройства', 'устройств']

describe('pluralRu', () => {
  it.each([
    [1, 'устройство'],
    [2, 'устройства'],
    [5, 'устройств'],
    [21, 'устройство'],
    [111, 'устройств'],
    [0, 'устройств'],
    [11, 'устройств'],
    [12, 'устройств'],
    [14, 'устройств'],
    [22, 'устройства'],
    [25, 'устройств'],
    [101, 'устройство'],
    [112, 'устройств'],
    [-1, 'устройство'],
    [-5, 'устройств']
  ])('%i -> %s', (n, expected) => {
    expect(pluralRu(n, forms)).toBe(expected)
  })
})

const idx = (n: number) => (n % 10 === 1 && n % 100 !== 11 ? 0 : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? 1 : 2)

describe('тройки таблицы plural', () => {
  for (const [key, triple] of Object.entries(table.plural)) {
    it(`${key}: 1, 2, 5, 11, 21, 0`, () => {
      for (const n of [1, 2, 5, 11, 21, 0]) {
        expect(pluralRu(n, triple as [string, string, string])).toBe(triple[idx(n)])
      }
      expect(pluralRu(1, triple as [string, string, string])).toBe(triple[0])
      expect(pluralRu(2, triple as [string, string, string])).toBe(triple[1])
      expect(pluralRu(5, triple as [string, string, string])).toBe(triple[2])
    })
  }
})
