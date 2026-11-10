import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDeviceAccess } from './useDeviceAccess'

// Эталон: запросы трёх прежних composable (useClientsWg, useClientsOvpn,
// useClientsIkev2) дословно - адрес и параметры $fetch.
const LEGACY = {
  wg: {
    config: ['/api/clients/7/devices/42/wg-config', { responseType: 'text' }],
    send: ['/api/clients/7/devices/42/send-wg-to-tg', { method: 'POST' }],
  },
  ovpn: {
    config: ['/api/clients/7/devices/42/ovpn-config', { responseType: 'text' }],
    send: ['/api/clients/7/devices/42/send-ovpn-to-tg', { method: 'POST' }],
  },
  ike: {
    config: ['/api/clients/7/devices/42/ikev2-credentials'],
    send: ['/api/clients/7/devices/42/send-ikev2-to-tg', { method: 'POST' }],
  },
} as const

describe('useDeviceAccess', () => {
  const fetchMock = vi.fn()
  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('$fetch', fetchMock)
  })
  afterEach(() => vi.unstubAllGlobals())

  for (const kind of ['wg', 'ovpn', 'ike'] as const) {
    it(`${kind}: конфиг запрашивается как раньше и ответ отдаётся без изменений`, async () => {
      const answer = kind === 'ike' ? { server: 'vpn.example', caCertPem: null } : '[Interface]\n'
      fetchMock.mockResolvedValueOnce(answer)
      expect(await useDeviceAccess(kind).getConfig(7, 42)).toBe(answer)
      expect(fetchMock.mock.calls).toEqual([LEGACY[kind].config])
    })

    it(`${kind}: отправка в Telegram идёт тем же запросом`, async () => {
      fetchMock.mockResolvedValueOnce({ ok: true })
      expect(await useDeviceAccess(kind).sendToTg(7, 42)).toEqual({ ok: true })
      expect(fetchMock.mock.calls).toEqual([LEGACY[kind].send])
    })

    it(`${kind}: ошибка сервера доходит до вызывающего`, async () => {
      const error = Object.assign(new Error('boom'), { statusMessage: 'Нет устройства' })
      fetchMock.mockRejectedValue(error)
      await expect(useDeviceAccess(kind).getConfig(7, 42)).rejects.toBe(error)
      await expect(useDeviceAccess(kind).sendToTg(7, 42)).rejects.toBe(error)
    })
  }

  it('адреса файлов постоянные и запросов не делают', () => {
    expect(useDeviceAccess('wg').qrUrl(7, 42)).toBe('/api/clients/7/devices/42/wg-qrcode.svg')
    expect(useDeviceAccess('ike').caCrtUrl()).toBe('/api/ikev2/ca.crt')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
