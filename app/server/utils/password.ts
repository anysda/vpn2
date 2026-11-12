import { randomInt } from 'node:crypto'

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

/** Пароль доступа клиента и IKEv2: только буквы, его вводят руками; randomInt выбирает без смещения. */
export function generatePassword(length = 20): string {
  return Array.from({ length }, () => ALPHABET[randomInt(ALPHABET.length)]).join('')
}
