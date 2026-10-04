import { fetchProxies, userVisibleOutbounds } from './clash-client'

// Маршрут на outbound, которого нет в sing-box (опечатка, снятый экзит), роутер
// не применит, а раньше API его молча сохранял. Сверяемся с тем же списком,
// что панель показывает в выборе outbound'а.
export async function assertKnownOutbound(outbound: string): Promise<void> {
  const names = userVisibleOutbounds(await fetchProxies()).map(p => p.name)
  if (names.length === 0) {
    throw createError({ statusCode: 503, statusMessage: 'outbounds_unavailable: sing-box clash-api is down' })
  }
  if (!names.includes(outbound)) {
    throw createError({ statusCode: 400, statusMessage: `unknown_outbound: ${outbound}`, data: { known: names } })
  }
}
