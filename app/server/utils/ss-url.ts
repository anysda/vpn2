export function buildSsUrl(opts: {
  cipher: string
  secret: string
  host: string
  port: number
  name: string
}): string {
  const userInfo = Buffer.from(`${opts.cipher}:${opts.secret}`).toString('base64')
  return `ss://${userInfo}@${opts.host}:${opts.port}#${encodeURIComponent(opts.name)}`
}
