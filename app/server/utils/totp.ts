import { createHmac, randomBytes } from 'node:crypto'

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

export function generateTotpSecret(): string {
  const bytes = randomBytes(20)
  let bits = 0
  let value = 0
  let out = ''
  for (const b of bytes) {
    value = (value << 8) | b
    bits += 8
    while (bits >= 5) {
      out += BASE32[(value >>> (bits - 5)) & 31]
      bits -= 5
    }
  }
  if (bits > 0) out += BASE32[(value << (5 - bits)) & 31]
  return out
}

function base32Decode(input: string): Buffer {
  const cleaned = input.toUpperCase().replace(/=+$/, '').replace(/\s/g, '')
  const bytes: number[] = []
  let bits = 0
  let value = 0
  for (const ch of cleaned) {
    const idx = BASE32.indexOf(ch)
    if (idx < 0) continue
    value = (value << 5) | idx
    bits += 5
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 0xff)
      bits -= 8
    }
  }
  return Buffer.from(bytes)
}

function hotpCode(key: Buffer, counter: number): string {
  const buf = Buffer.alloc(8)
  buf.writeBigUInt64BE(BigInt(counter))
  const hmac = createHmac('sha1', key).update(buf).digest()
  const offset = hmac[hmac.length - 1]! & 0x0f
  const code
    = ((hmac[offset]! & 0x7f) << 24)
      | ((hmac[offset + 1]! & 0xff) << 16)
      | ((hmac[offset + 2]! & 0xff) << 8)
      | (hmac[offset + 3]! & 0xff)
  return (code % 1_000_000).toString().padStart(6, '0')
}

export function verifyTotpToken(token: string, secret: string, window = 1): boolean {
  const cleaned = token.replace(/\s/g, '')
  if (!/^\d{6}$/.test(cleaned)) return false
  const key = base32Decode(secret)
  const t = Math.floor(Date.now() / 1000 / 30)
  for (let i = -window; i <= window; i++) {
    if (hotpCode(key, t + i) === cleaned) return true
  }
  return false
}

export function buildTotpUri(username: string, secret: string): string {
  const label = encodeURIComponent(`anysda-vpn2:${username}`)
  return `otpauth://totp/${label}?secret=${secret}&issuer=anysda-vpn2&algorithm=SHA1&digits=6&period=30`
}
