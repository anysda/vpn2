import { describe, expect, it } from 'vitest'
import pkg from '../../package.json' with { type: 'json' }
import { markPanelReady } from '../utils/api'
import { installStubs } from './stubs'

installStubs()
const handler = (await import('../resources/ops')).default['GET /version'] as unknown as () => unknown

describe('GET /api/version', () => {
  it('номер версии - из app/package.json, единственного места выпуска', () => {
    markPanelReady()
    expect(handler()).toEqual({ name: 'anysda-vpn2', version: pkg.version, license: 'MIT' })
  })
})
