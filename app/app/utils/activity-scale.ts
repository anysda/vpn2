/** Ступень дня 0..4 по квартилям ненулевых дней периода; различимых значений не больше четырёх - каждому своя, наибольшее темнее всех. */
export function activityScale(values: number[]): (v: number) => number {
  const s = values.filter(v => v > 0).sort((a, b) => a - b)
  const u = [...new Set(s)]
  if (u.length <= 4) {
    const step = new Map(u.map((v, i) => [v, u.length === 1 ? 4 : 1 + Math.round(i * 3 / (u.length - 1))]))
    return v => v > 0 ? step.get(v) ?? 4 : 0
  }
  const th = [1, 2, 3].map(k => s[Math.floor(k * s.length / 4)]!)
  return v => v > 0 ? 1 + th.filter(t => v >= t).length : 0
}
