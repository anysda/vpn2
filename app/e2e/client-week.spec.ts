import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { openClient } from './support/clients'
import { container, inside } from './support/openvpn-ca'
import { weekByMskDay } from '../server/utils/client-week'
import { mskDayStartSec } from '../server/utils/msk-day'

const GIB = 1024 ** 3
const sec = (iso: string) => Date.parse(iso) / 1000

test('неделя клиента: часы раскладываются по московским суткам', () => {
  const now = Date.parse('2026-10-14T12:00:00Z')
  const week = weekByMskDay([
    { hour: sec('2026-10-13T20:00:00Z'), rx: 1, tx: 10 }, // 23:00 МСК 13-го
    { hour: sec('2026-10-13T21:00:00Z'), rx: 2, tx: 20 }, // 00:00 МСК 14-го — уже сегодня
    { hour: sec('2026-10-13T21:00:00Z'), rx: 3, tx: 30 }, // второй девайс в тот же час
    { hour: sec('2026-10-07T21:00:00Z'), rx: 4, tx: 40 }, // начало окна, 8-е по МСК
    { hour: sec('2026-10-07T20:00:00Z'), rx: 99, tx: 99 }, // 7-е по МСК — вне окна
  ], now)
  expect(week).toEqual([
    { date: '2026-10-08', rx: 4, tx: 40 },
    { date: '2026-10-09', rx: 0, tx: 0 },
    { date: '2026-10-10', rx: 0, tx: 0 },
    { date: '2026-10-11', rx: 0, tx: 0 },
    { date: '2026-10-12', rx: 0, tx: 0 },
    { date: '2026-10-13', rx: 1, tx: 10 },
    { date: '2026-10-14', rx: 5, tx: 50 },
  ])
})

test('неделя клиента: сумма по девайсам в API и на графике', async ({ page }) => {
  test.skip(!container)
  await loginApi(page)
  const name = uniq('week')
  const created = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(created.status()).toBe(200)
  const { id } = await created.json()
  const devices: number[] = []
  for (const d of ['phone', 'laptop']) {
    const r = await page.request.post(`/api/clients/${id}/devices`, { data: { name: d } })
    expect(r.status()).toBe(200)
    devices.push((await r.json()).id)
  }

  const today = mskDayStartSec(Date.now(), 0)
  const first = mskDayStartSec(Date.now(), 6)
  inside(`import { DatabaseSync } from 'node:sqlite';
    const [rows] = process.argv.slice(1).map(a => JSON.parse(a));
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    db.exec('PRAGMA busy_timeout = 10000');
    const add = db.prepare('INSERT INTO device_traffic_hourly (device_id, hour, rx, tx) VALUES (?, ?, ?, ?)');
    for (const r of rows) add.run(...r);`, [
    [devices[0], today, GIB / 2, 1000],
    [devices[1], today, GIB / 2, 2000],
    [devices[0], first, 5000, 6000],
    [devices[1], first - 3600, 7, 7],
  ])

  const r = await page.request.get(`/api/clients/${id}/traffic`)
  expect(r.status()).toBe(200)
  const week: { rx: number, tx: number }[] = await r.json()
  expect(week).toHaveLength(7)
  expect(week[6]).toMatchObject({ rx: GIB, tx: 3000 })
  expect(week[0]).toMatchObject({ rx: 5000, tx: 6000 })
  expect(week.slice(1, 6).every(d => d.rx === 0 && d.tx === 0)).toBe(true)

  await page.goto('/clients')
  await hydrated(page)
  const panel = await openClient(page, name)
  await expect(panel.getByTestId('week-day').last()).toHaveAttribute('title', '↓ 1,0 ГБ · ↑ 3 КБ')
  expect((await page.request.get('/api/clients/999999999/traffic')).status()).toBe(404)
})
