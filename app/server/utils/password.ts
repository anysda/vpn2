import { randomInt } from 'node:crypto'

// Буквы верхнего и нижнего регистра — без цифр и спецсимволов.
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

/**
 * Пароль доступа клиента — 20 символов из букв двух регистров.
 * randomInt — криптостойкий несмещённый выбор индекса.
 */
export function generateClientPassword(length = 20): string {
  let out = ''
  for (let i = 0; i < length; i++) out += ALPHABET[randomInt(ALPHABET.length)]
  return out
}
