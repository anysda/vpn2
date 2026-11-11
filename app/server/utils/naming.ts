// Кириллица → латиница для слагов имён клиентов/девайсов.
const CYR: Record<string, string> = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh',
  з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o',
  п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts',
  ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
}

/** Имя → слаг латиницей: «Артём Фёдоров» → «artem-fedorov», «iPhone 14» → «iphone-14». */
export function slugify(s: string): string {
  const latin = s.trim().toLowerCase().replace(/[Ѐ-ӿ]/g, ch => CYR[ch] ?? '')
  return latin.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'x'
}

/** Имя файла конфига и подпись в Telegram: до 15 ASCII-символов (имя интерфейса wg-quick), суффикс из ID различает устройства. */
export function deviceConfigName(clientName: string, deviceName: string, deviceId: number): string {
  const suffix = `-${deviceId.toString(36)}`
  const label = `${slugify(clientName)}-${slugify(deviceName)}`
  return `${label.slice(0, 15 - suffix.length)}${suffix}`
}
