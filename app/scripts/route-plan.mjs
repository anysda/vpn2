import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const infraTests = fileURLToPath(new URL('../../infra/tests', import.meta.url))

// Снимок строит тот же скрипт роутера, что пишет его на сервере: генератор
// конфига и anysda-apply-routes.py из стадии 20. Проверки sing-box в нём
// заменены на `true`, наборы geoip/geosite - тестовые.
const BUILD = `
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import router_support as rs
d, a = Path(sys.argv[2]), json.loads(sys.argv[3])
script = rs.prepare(d, **a['env'])
for tag, rules in a['sets'].items():
    (d / (tag + '.json')).write_text(json.dumps({'version': 2, 'rules': rules}))
(d / 'manual.json').write_text(json.dumps(a['rows']))
(d / 'prefs.json').write_text(json.dumps(a['prefs']))
r = rs.apply(script)
if r.returncode:
    sys.exit(r.stderr)
`

/** План маршрутов для тестов панели: строки базы, наборы правил, окружение роутера, предпочтения. */
export function buildRoutePlan(rows, sets, env = {}, prefs = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'route-plan-'))
  try {
    execFileSync('python3', ['-c', BUILD, infraTests, dir, JSON.stringify({ rows, sets, env, prefs })], {
      env: { ...process.env, SING_BOX_BIN: 'true' },
    })
    return JSON.parse(readFileSync(join(dir, 'plan.json'), 'utf8'))
  }
  finally {
    rmSync(dir, { recursive: true, force: true })
  }
}
