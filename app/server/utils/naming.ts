// Кириллица → латиница для слагов имён клиентов/девайсов.
const CYR: Record<string, string> = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh',
  з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o',
  п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts',
  ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
}

/**
 * Имя → слаг латиницей: кириллица транслитерируется, регистр опускается,
 * пробелы и прочие символы → «-», повторы схлопываются. Английские имена
 * остаются как есть (без транслита). «Артём Фёдоров» → «artem-fedorov»,
 * «John» → «john», «iPhone 14» → «iphone-14». Имена без пробелов — без тире.
 */
export function slugify(s: string): string {
  const latin = s.trim().toLowerCase().replace(/[Ѐ-ӿ]/g, ch => CYR[ch] ?? '')
  return latin.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'x'
}

/**
 * Общее имя файла (.conf / .ovpn) и подпись при отправке в Telegram.
 * wg-quick использует его как имя интерфейса: максимум 15 ASCII-символов.
 * Суффикс из ID различает устройства даже при совпадении/сокращении слагов.
 */
export function deviceConfigName(clientName: string, deviceName: string, deviceId: number): string {
  const suffix = `-${deviceId.toString(36)}`
  const label = `${slugify(clientName)}-${slugify(deviceName)}`
  return `${label.slice(0, 15 - suffix.length)}${suffix}`
}
