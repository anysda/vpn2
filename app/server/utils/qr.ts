import { crc32, deflateSync } from 'node:zlib'
import QRCode from 'qrcode-svg'

const pad = 4

// ecl 'L': сплит-туннель раздувает конфиг до ~950 байт и больше, при 'M' QR
// получается настолько плотным, что телефон его с экрана уже не берёт.
function qrMatrix(content: string): boolean[][] {
  const qr = new QRCode({ content, padding: 0, ecl: 'L' }) as unknown as {
    qrcode: { modules: boolean[][] }
  }
  return qr.qrcode.modules
}

/**
 * Свой вывод вместо `.svg()` библиотеки: та рисует отдельный <rect> со стилем
 * на каждый модуль и с дробными координатами (512px / N модулей), и на таком
 * конфиге выходил мегабайт. Здесь один path в единицах модуля: подряд идущие
 * тёмные модули строки сливаются в один отрезок. Свой размер - 4 px на модуль:
 * меньше камера телефона с экрана ПК не берёт; уже контента ужимает CSS.
 */
export function qrSvg(content: string): string {
  const m = qrMatrix(content)
  const n = m.length
  let d = ''
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      if (!m[y]![x]) continue
      const x0 = x
      while (x + 1 < n && m[y]![x + 1]) x++
      d += `M${x0 + pad} ${y + pad}h${x - x0 + 1}v1h-${x - x0 + 1}z`
    }
  }
  const size = n + 2 * pad
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size * 4}" height="${size * 4}" shape-rendering="crispEdges">`
    + `<rect width="${size}" height="${size}" fill="#fff"/><path d="${d}"/></svg>`
}

function chunk(type: string, data: Buffer): Buffer {
  const head = Buffer.alloc(8)
  head.writeUInt32BE(data.length, 0)
  head.write(type, 4, 'ascii')
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0)
  return Buffer.concat([head, data, crc])
}

/**
 * PNG для Telegram, как рисовал бот: чёрное на белом, поле 4 модуля, модуль -
 * наибольший из 1..10 px, при котором сторона не больше 1280 px (больше
 * Telegram фото ужимает). 8-битный серый, строки без фильтра.
 */
export function qrPng(content: string): Buffer {
  const m = qrMatrix(content)
  const n = m.length
  const box = Math.max(1, Math.min(10, Math.floor(1280 / (n + 2 * pad))))
  const side = (n + 2 * pad) * box
  const row = side + 1
  const raw = Buffer.alloc(row * side, 0xFF)
  for (let y = 0; y < side; y++) {
    raw[y * row] = 0
    const my = Math.floor(y / box) - pad
    if (my < 0 || my >= n) continue
    for (let mx = 0; mx < n; mx++) {
      if (m[my]![mx]) raw.fill(0, y * row + 1 + (mx + pad) * box, y * row + 1 + (mx + pad + 1) * box)
    }
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(side, 0)
  ihdr.writeUInt32BE(side, 4)
  ihdr[8] = 8 // глубина
  ihdr[9] = 0 // серый
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ])
}
