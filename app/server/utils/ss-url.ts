// DPI-маскировка Shadowsocks-ключей.
//
// Плейн-Shadowsocks — поток высокоэнтропийных байт без рукопожатия; DPN/DPI
// (в т.ч. РФ TSPU) классифицирует его как «fully-encrypted proxy» и режет.
// `prefix` задаёт первые байты СОЛИ SS-соединения — соль идёт открытым
// текстом в начале потока, поэтому соединение начинается с заданных байт.
// Здесь префикс имитирует начало TLS-handshake (record 0x16 0x03 0x01 …,
// ClientHello), и поток выглядит как обычный HTTPS.
//
// Это ЧИСТО клиентская опция: outline-ss-server принимает любую соль, менять
// сервер не нужно. Префикс-aware клиент (приложение Outline) применяет его,
// остальные просто игнорируют query и подключаются как обычно.
//
// Это частичная мера: маскируется только начало потока, энтропийный DPI по
// всему flow остаётся. Полная защита — TLS-обёртка (shadow-tls) или смена
// протокола. См. https://developer.getoutline.org/vpn/advanced/prefixing
//
// URL-форма префикса (Outline-клиент: url-decode → utf-8 → младший байт):
//   %16%03%01%00%C2%A8%01%01 → байты 16 03 01 00 a8 01 01 (TLS ClientHello)
const SS_DISGUISE_PREFIX = '%16%03%01%00%C2%A8%01%01'

export function buildSsUrl(opts: {
  cipher: string
  secret: string
  host: string
  port: number
  name: string
}): string {
  const userInfo = Buffer.from(`${opts.cipher}:${opts.secret}`).toString('base64')
  return `ss://${userInfo}@${opts.host}:${opts.port}/?outline=1&prefix=${SS_DISGUISE_PREFIX}#${encodeURIComponent(opts.name)}`
}
