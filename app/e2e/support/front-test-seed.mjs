// Наполнение панели для ./setup.sh --front-test: клиенты, устройства, маршруты,
// бот - через API панели (ключи WG, сертификаты OpenVPN и логины IKEv2 выдаёт
// она сама), а то, чего API не позволяет (просроченный срок, привязка Telegram,
// давность, история трафика), - через схему БД панели.
//
// Запускается в контейнере панели (e2e/front-test.sh кладёт его и schema.ts в
// /app/.output/server/front-test, к модулям сервера; в dev-режиме - прямо из app/):
//   PANEL_URL, ADMIN_PASSWORD, DB_URL, LIVE_FILE, EXIT_TAGS
import { writeFileSync } from 'node:fs'
import { createClient } from '@libsql/client'
import { eq } from 'drizzle-orm'
import { drizzle } from 'drizzle-orm/libsql'
import { clients, collectorState, deviceTrafficHourly, devices } from '../../server/database/schema.ts'

const PANEL = process.env.PANEL_URL || 'http://panel:51821'
const EXITS = (process.env.EXIT_TAGS || 'nl fi us').split(/\s+/).filter(Boolean)
const HEALTHY = EXITS.slice(0, -1).length ? EXITS.slice(0, -1) : EXITS
const DAY = 86400
const now = Math.floor(Date.now() / 1000)

// Детерминированный ГПСЧ: при перезапуске картина та же, меняется только «сейчас».
let rnd = 20251011
function rand() {
  rnd = (rnd * 1103515245 + 12345) % 2147483648
  return rnd / 2147483648
}

// expires - дней от сегодня (отрицательное - просрочен), null - бессрочно.
// scale - сколько качает относительно среднего; online - есть трафик сейчас.
const PEOPLE = [
  { name: 'Анна Смирнова', devices: ['iPhone 15', 'MacBook Air'], expires: 180, tg: 'anna_smirnova', online: true, scale: 1.2, age: 210 },
  { name: 'Дмитрий Козлов', devices: ['Pixel 8', 'Ноутбук', 'iPad'], expires: 95, tg: 'dkozlov', online: true, scale: 3, age: 160 },
  { name: 'Офис Алматы', devices: ['Роутер Keenetic', 'Бухгалтерия ПК'], expires: null, limit: 10, online: true, scale: 4.5, age: 300 },
  { name: 'Мама', devices: ['Samsung A54'], expires: null, tg: 'lyudmila_k', online: false, scale: 0.4, age: 260, idleHours: 5 },
  { name: 'Сергей Волков', devices: ['iPhone 13'], expires: 3, tg: 'volkov_s', online: true, scale: 0.9, age: 85 },
  { name: 'Екатерина Белова', devices: ['iPhone 14', 'Apple TV'], expires: -12, tg: 'katya_belova', online: false, scale: 1.6, age: 190 },
  { name: 'Игорь Петренко', devices: ['Android', 'Windows ПК'], expires: 2, online: true, scale: 1.1, age: 30 },
  { name: 'Ольга Новикова', devices: ['iPhone 12'], expires: -40, online: false, scale: 0.7, age: 230 },
  { name: 'Максим Орлов', devices: ['Xiaomi 13', 'Steam Deck', 'Ноутбук'], expires: 60, tg: 'max_orlov', frozen: true, online: false, scale: 2.2, age: 120, idleHours: 70 },
  { name: 'Наталья Иванова', devices: ['iPad Pro'], expires: 365, online: true, scale: 0.3, age: 45 },
  { name: 'Артём Фёдоров', devices: ['iPhone 15 Pro', 'MacBook Pro'], expires: 30, tg: 'artem_f', exit: HEALTHY[0], online: true, scale: 2.6, age: 140 },
  { name: 'Павел Соколов', devices: ['Android TV'], expires: 6, online: false, scale: 1.8, age: 70, idleHours: 26 },
  { name: 'Дача', devices: ['Роутер'], expires: null, online: true, scale: 0.15, age: 400 },
  { name: 'Юлия Морозова', devices: ['iPhone 11', 'Ноутбук'], expires: -3, tg: 'yulia_moroz', online: false, scale: 1, age: 95 },
  { name: 'Константин Лебедев', devices: ['Linux сервер'], expires: 120, exit: HEALTHY[1] ?? HEALTHY[0], online: true, scale: 1.4, age: 55 },
  { name: 'Алексей (тест)', devices: ['Android'], expires: 1, frozen: true, online: false, scale: 0.05, age: 4, idleHours: 50 },
  { name: 'Виктория Захарова', devices: ['iPhone 14', 'iPad'], expires: 45, tg: 'vika_z', online: true, scale: 1.3, age: 100 },
  { name: 'Роман Ким', devices: ['Windows ПК'], expires: 200, exit: EXITS.at(-1), online: false, scale: 0.8, age: 75, idleHours: 9 },
]

const ROUTES = [
  ['gosuslugi.ru', 'direct-ru'], ['sberbank.ru', 'direct-ru'], ['ozon.ru', 'direct-ru'], ['vk.com', 'direct-ru'],
  ['77.88.8.8/32', 'direct-ru'],
  ['netflix.com', 0], ['*.openai.com', 0], ['chatgpt.com', 0], ['claude.ai', 0], ['spotify.com', 0],
  ['gemini.google.com', '0w'],
  ['youtube.com', 1], ['googlevideo.com', 1], ['instagram.com', 1], ['149.154.160.0/20', 1],
  ['x.com', '1w'],
  ['hulu.com', 2], ['disneyplus.com', 2],
]

let cookie = ''
async function api(method, path, body) {
  const res = await fetch(PANEL + path, {
    method,
    // Origin - как у браузера: ручки входа без него POST не принимают.
    headers: { 'content-type': 'application/json', 'origin': new URL(PANEL).origin, cookie },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const set = res.headers.getSetCookie()
  if (set.length) cookie = set.map(c => c.split(';')[0]).join('; ')
  const text = await res.text()
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${text}`)
  return text ? JSON.parse(text) : null
}

function routeOutbound(spec) {
  if (typeof spec === 'string' && !/^\d/.test(spec)) return spec
  const s = String(spec)
  const tag = EXITS[Number(s[0])]
  return tag ? `hy2-${tag}-${s.endsWith('w') ? 'warp' : 'direct'}` : null
}

const isoDays = d => new Date((now + d * DAY) * 1000).toISOString()

// Доля суточного трафика по часу МСК: ночью тихо, вечером пик.
const CURVE = [3, 2, 1.5, 1, 1, 1, 1.5, 3, 5, 6, 6, 6.5, 7, 7, 6.5, 6.5, 7, 8, 9.5, 11, 12, 12, 10, 6]

async function main() {
  await api('POST', '/api/auth/sign-in/username', { username: 'admin', password: process.env.ADMIN_PASSWORD })

  await api('PUT', '/api/admin/telegram', {
    bot_token: '0000000000:front-test-placeholder',
    chat_id: '-1002318845510',
    admin_username: 'anysda_support',
  })

  for (const [value, spec] of ROUTES) {
    const outbound = routeOutbound(spec)
    if (outbound) await api('POST', '/api/routes', { value, outbound })
  }

  const created = []
  for (const p of PEOPLE) {
    const c = await api('POST', '/api/clients', {
      name: p.name,
      filterTraffic: p.name !== 'Офис Алматы',
      expiresAt: p.expires != null && p.expires > 0 ? isoDays(p.expires) : null,
      deviceLimit: p.limit ?? Math.max(3, p.devices.length),
    })
    for (const d of p.devices) await api('POST', `/api/clients/${c.id}/devices`, { name: d })
    const patch = {}
    if (p.frozen) patch.frozenManual = true
    if (p.exit) patch.preferredExit = p.exit
    if (Object.keys(patch).length) await api('PATCH', `/api/clients/${c.id}`, patch)
    created.push({ ...p, id: c.id })
  }

  const db = drizzle(createClient({ url: process.env.DB_URL }))
  const hourly = []
  const live = []
  for (const p of created) {
    const createdAt = new Date((now - p.age * DAY) * 1000)
    await db.update(clients).set({
      createdAt,
      updatedAt: createdAt,
      ...(p.expires != null && p.expires <= 0 ? { expiresAt: new Date((now + p.expires * DAY) * 1000) } : {}),
      ...(p.tg ? { tgChatId: 100000000 + Math.floor(rand() * 899999999), tgUsername: p.tg } : {}),
    }).where(eq(clients.id, p.id))

    const devs = await db.select().from(devices).where(eq(devices.clientId, p.id))
    // Последний час с трафиком: просроченным - день истечения, остальным - простой.
    const lastHour = p.expires != null && p.expires <= 0
      ? now + p.expires * DAY
      : now - (p.idleHours ?? 0) * 3600
    for (const [i, d] of devs.entries()) {
      const share = i === 0 ? 1 : 0.35 + rand() * 0.4
      let rx = 0
      let tx = 0
      // 14 суток (не раньше появления клиента): неделя в ТОПе считается целиком.
      const from = now - Math.min(14, p.age) * DAY
      for (let h = Math.floor(from / 3600) * 3600; h <= lastHour; h += 3600) {
        const mskHour = Math.floor(((h + 3 * 3600) % DAY) / 3600)
        const part = h + 3600 > now ? (now - h) / 3600 : 1
        const bytes = Math.round(p.scale * share * CURVE[mskHour] * 9e6 * (0.5 + rand()) * part)
        if (bytes < 1e5) continue
        const up = Math.round(bytes * (0.1 + rand() * 0.15))
        hourly.push({ deviceId: d.id, hour: h, rx: bytes, tx: up })
        rx += bytes
        tx += up
      }
      // Трафик до этих двух недель - только в накопительном итоге.
      const before = Math.round(p.scale * share * Math.min(p.age, 400) * 1.1e9 * (0.6 + rand() * 0.8))
      const devCreated = new Date((now - Math.max(1, p.age - i * 9) * DAY) * 1000)
      await db.update(devices).set({
        rxTotal: rx + before,
        txTotal: tx + Math.round(before * 0.16),
        createdAt: devCreated,
        updatedAt: devCreated,
      }).where(eq(devices.id, d.id))
      if (p.online && (i === 0 || rand() < 0.4) && d.ikev2Username && d.ikev2Ip) {
        live.push({
          id: d.id,
          username: d.ikev2Username,
          ip: d.ikev2Ip,
          exit: p.exit && HEALTHY.includes(p.exit) ? p.exit : HEALTHY[d.id % HEALTHY.length],
          rateBps: Math.round(p.scale * (15000 + rand() * 180000)),
        })
      }
    }
  }
  for (let i = 0; i < hourly.length; i += 500) {
    await db.insert(deviceTrafficHourly).values(hourly.slice(i, i + 500)).onConflictDoNothing()
  }
  // Пустой, но существующий снимок IKEv2: сборщик засчитает живые SA с первого
  // же прогона, а не только базируется на них.
  await db.insert(collectorState).values({ name: 'ikev2', value: '{}' })
    .onConflictDoUpdate({ target: collectorState.name, set: { value: '{}' } })
  writeFileSync(process.env.LIVE_FILE, JSON.stringify({ devices: live }))

  console.log(`клиентов ${created.length}, устройств ${created.reduce((a, p) => a + p.devices.length, 0)}, `
    + `маршрутов ${ROUTES.length}, часов истории ${hourly.length}, в сети устройств ${live.length}`)

  // Сборщик трафика панели ходит раз в минуту: ждём, пока клиенты станут «в сети».
  // Ожидание необязательное, сбой запроса его не роняет. Запись в БД выше
  // синхронная и держит цикл событий дольше keep-alive сервера (5 с): первый
  // запрос уходит в уже закрытое соединение, «other side closed».
  let failed = ''
  for (let i = 0; i < 40; i++) {
    try {
      const top = await api('GET', '/api/clients/top')
      if (top.counts.onlineNow > 0) {
        console.log(`в сети клиентов: ${top.counts.onlineNow}`)
        return
      }
      failed = ''
    }
    catch (e) {
      failed = e.cause?.message || e.message
    }
    await new Promise(r => setTimeout(r, 3000))
  }
  console.log(`клиенты пока не в сети${failed ? ` (${failed})` : ''}: `
    + 'сборщик трафика панели не успел, обновите страницу через минуту')
}

await main()
