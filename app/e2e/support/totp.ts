import { createHmac } from 'node:crypto'

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

function base32Decode(input: string): Buffer {
  const bytes: number[] = []
  let bits = 0
  let value = 0
  for (const ch of input.toUpperCase().replace(/[\s=]/g, '')) {
    const idx = BASE32.indexOf(ch)
    if (idx < 0) continue
    value = (value << 5) | idx
    bits += 5
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 0xFF)
      bits -= 8
    }
  }
  return Buffer.from(bytes)
}

/** RFC 6238, SHA1, 6 цифр, шаг 30 с — как у панели и у аутентификаторов. */
export function totp(secret: string, at = Date.now()): string {
  const buf = Buffer.alloc(8)
  buf.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / 30)))
  const h = createHmac('sha1', base32Decode(secret)).update(buf).digest()
  const o = h[h.length - 1]! & 0x0F
  const code = ((h[o]! & 0x7F) << 24) | (h[o + 1]! << 16) | (h[o + 2]! << 8) | h[o + 3]!
  return (code % 1_000_000).toString().padStart(6, '0')
}
