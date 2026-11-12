import { randomInt } from 'node:crypto'

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

/** Пароль доступа клиента: только буквы, randomInt выбирает без смещения. */
export function generateClientPassword(length = 20): string {
  return Array.from({ length }, () => ALPHABET[randomInt(ALPHABET.length)]).join('')
}
