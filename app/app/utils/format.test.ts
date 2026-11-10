import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import {
  bytesToGbDec, ddmmLocal, dateRuLocal, expiryLabelLocal, formatRuDateUtc, gbDecNumRu, gbDecRound1, gbDecRu,
  gibLatin, gibTotalLatin, hmsOrDdmmHmLocal, hourRangeRu, mskDayNumLabel, mskHourFromSec, mskWeekdayDateFromSec,
  pad2, parseRuDateUtc, sizeBinRu, timeOrDdmmMsk, todayYesterdayHmLocal, WD_MON,
} from './format'

// Ожидаемые значения сняты прогоном прежних функций из компонентов в TZ=UTC и TZ=Europe/Moscow.
const GIB = 1024 ** 3
const MIB = 1024 * 1024

describe('объём', () => {
  it('sizeBinRu: база 1024, ГБ/МБ/КБ', () => {
    const in_ = [null, undefined, 0, -5, 1, 1023, 1536, MIB - 1, MIB, 380 * MIB, GIB - 1, GIB, 1.4 * GIB]
    expect(in_.map(sizeBinRu)).toEqual(['—', '—', '—', '—', '1 КБ', '1 КБ', '2 КБ', '1024 КБ', '1 МБ', '380 МБ', '1024 МБ', '1,0 ГБ', '1,4 ГБ'])
  })
  it('gbDecRu: база 1e9, знак до 1e10', () => {
    const in_ = [null, undefined, 0, -2e9, 5e8, 1234567890, 1e10 - 1, 1e10, 12345678901, 1.5e12]
    expect(in_.map(gbDecRu)).toEqual(['—', '—', '0,0 ГБ', '-2,0 ГБ', '0,5 ГБ', '1,2 ГБ', '10,0 ГБ', '10 ГБ', '12 ГБ', '1 500 ГБ'])
    expect([0, 1e10 - 1, 1e10, 1.5e12].map(gbDecNumRu)).toEqual(['0,0', '10,0', '10', '1 500'])
    expect(bytesToGbDec(2.5e9)).toBe(2.5)
  })
  it('gbDecRound1: ГБ до 0,1 числом', () => {
    expect([0, 4e7, 5e7, 1e9, 1.26e9, 12345678901].map(gbDecRound1)).toEqual([0, 0, 0.1, 1, 1.3, 12.3])
  })
  it('gibLatin / gibTotalLatin: диск, база 1024, латиница', () => {
    expect([0, GIB, 10.4 * GIB, 10.6 * GIB].map(gibLatin)).toEqual(['0 GB', '1 GB', '10 GB', '11 GB'])
    expect(gibTotalLatin(0, 0)).toBe('—')
    expect(gibTotalLatin(GIB, null)).toBe('—')
    expect(gibTotalLatin(10.4 * GIB, 25)).toBe('42 GB')
    expect(gibTotalLatin(10.6 * GIB, 33.3)).toBe('32 GB')
  })
})

for (const tz of ['UTC', 'Europe/Moscow']) {
  const msk = tz === 'Europe/Moscow'
  describe(`даты, TZ=${tz}`, () => {
    let was: string | undefined
    beforeAll(() => { was = process.env.TZ; process.env.TZ = tz })
    afterAll(() => { process.env.TZ = was })

    it('зона процесса применилась', () => {
      expect(new Date(Date.UTC(2026, 0, 1)).getTimezoneOffset()).toBe(msk ? -180 : 0)
    })
    it('parseRuDateUtc / formatRuDateUtc: UTC, от зоны не зависят', () => {
      expect(['31.12.2026', ' 1.2.2026 ', '31.02.2026', 'abc', '2026-12-31', ''].map(parseRuDateUtc))
        .toEqual(['2026-12-31T00:00:00.000Z', '2026-02-01T00:00:00.000Z', null, null, null, null])
      expect([null, undefined, '', 'bad', '2026-12-31T00:00:00.000Z', '2026-12-31T21:00:00.000Z', '2026-12-31T23:59:59.000Z'].map(formatRuDateUtc))
        .toEqual(['', '', '', '', '31.12.2026', '31.12.2026', '31.12.2026'])
    })
    it('expiryLabelLocal / dateRuLocal: зона браузера', () => {
      expect([null, undefined, '', '2026-12-31T00:00:00.000Z', '2026-12-30T21:00:00.000Z', '2026-12-31T22:30:00.000Z'].map(expiryLabelLocal))
        .toEqual(['Бессрочный', 'Бессрочный', 'Бессрочный', 'до 31.12.2026', msk ? 'до 31.12.2026' : 'до 30.12.2026', msk ? 'до 01.01.2027' : 'до 31.12.2026'])
      expect(['2026-12-31T00:00:00.000Z', '2026-12-30T21:00:00.000Z', '2026-12-31T22:30:00.000Z'].map(dateRuLocal))
        .toEqual(['31.12.2026', msk ? '31.12.2026' : '30.12.2026', msk ? '01.01.2027' : '31.12.2026'])
    })
    it('timeOrDdmmMsk: МСК при любой зоне, «вчера» нет', () => {
      const now = Date.UTC(2026, 10, 10, 12, 0)
      expect([now - 3600e3, Date.UTC(2026, 10, 9, 21, 0), Date.UTC(2026, 10, 9, 20, 59), Date.UTC(2026, 10, 10, 0, 30)].map(m => timeOrDdmmMsk(m, now)))
        .toEqual(['14:00', '00:00', '09.11', '03:30'])
    })
    it('Wave: час и дата МСК из секунд начала', () => {
      const s = [Date.UTC(2026, 10, 9, 21, 0) / 1000, Date.UTC(2026, 10, 10, 20, 0) / 1000, Date.UTC(2026, 11, 31, 22, 0) / 1000]
      expect(s.map(x => hourRangeRu(mskHourFromSec(x)))).toEqual(['00:00–01:00', '23:00–00:00', '01:00–02:00'])
      expect(s.map(mskWeekdayDateFromSec)).toEqual(['вт, 10 ноя', 'вт, 10 ноя', 'пт, 1 янв'])
    })
    it('Activity: подпись суток МСК по номеру', () => {
      expect([0, 20402, 20453].map(mskDayNumLabel)).toEqual(['чт, 1 янв', 'пн, 10 ноя', 'ср, 31 дек'])
      expect(WD_MON[0]).toBe('пн')
    })
    it('todayYesterdayHmLocal: сегодня/вчера в зоне браузера', () => {
      const now = () => new Date(2026, 10, 10, 12, 0)
      const in_ = [new Date(2026, 10, 10, 0, 0), new Date(2026, 10, 9, 23, 59), new Date(2026, 10, 9, 0, 0), new Date(2026, 10, 8, 23, 59)].map(d => d.toISOString())
      expect([...in_, '2026-11-09T21:00:00.000Z'].map(u => todayYesterdayHmLocal(u, now())))
        .toEqual(['сегодня 00:00', 'вчера 23:59', 'вчера 00:00', '08.11 23:59', msk ? 'сегодня 00:00' : 'вчера 21:00'])
    })
    it('hmsOrDdmmHmLocal / ddmmLocal / pad2', () => {
      const today = new Date(2026, 10, 10, 12, 0).toDateString()
      const in_ = [new Date(2026, 10, 10, 0, 0, 5), new Date(2026, 10, 9, 23, 59, 59)].map(d => d.toISOString())
      expect([...in_, '2026-11-09T21:00:00.000Z'].map(t => hmsOrDdmmHmLocal(t, today)))
        .toEqual(['00:00:05', '09.11 23:59', msk ? '00:00:00' : '09.11 21:00'])
      expect([new Date(2026, 0, 5), new Date(2026, 11, 31)].map(ddmmLocal)).toEqual(['05.01', '31.12'])
      expect([0, 7, 12].map(pad2)).toEqual(['00', '07', '12'])
    })
  })
}
