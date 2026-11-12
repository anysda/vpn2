import table from './error-text.json' with { type: 'json' }

export type PluralKey = keyof typeof table.plural

/** Форма существительного после целого числа: один, два, пять. @internal */
export function pluralRu(n: number, forms: [string, string, string]): string {
  const abs = Math.abs(n)
  const mod10 = abs % 10
  const mod100 = abs % 100
  if (mod10 === 1 && mod100 !== 11) return forms[0]
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return forms[1]
  return forms[2]
}

/** Форма по ключу общей таблицы `plural` (её же читает бот). */
export function plural(n: number, key: PluralKey): string {
  return pluralRu(n, table.plural[key] as [string, string, string])
}

export const inDaysRu = (n: number) => `через ${n} ${plural(n, 'day')}`
