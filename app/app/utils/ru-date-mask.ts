/** Цифры из ввода → строка ДД.ММ.ГГГГ. Точки — только между группами,
 *  без хвостовой: иначе backspace «залипает» на точке. */
export function maskRuDate(raw: string): string {
  const d = raw.replace(/\D/g, '').slice(0, 8)
  if (d.length <= 2) return d
  if (d.length <= 4) return `${d.slice(0, 2)}.${d.slice(2)}`
  return `${d.slice(0, 2)}.${d.slice(2, 4)}.${d.slice(4)}`
}
