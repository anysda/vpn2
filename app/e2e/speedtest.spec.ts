import { execFileSync } from 'node:child_process'
import { test } from './support/fixtures'

test('speedtest меняет экзит через API панели с Origin', () => {
  console.log(execFileSync('python3', ['../infra/tests/test_speedtest.py', 'LivePanelTest', '-v'], {
    encoding: 'utf8', timeout: 60_000,
  }))
})
