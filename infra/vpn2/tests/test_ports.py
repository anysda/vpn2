"""Проверка портов hy2 после 20-ru-router: эталон - вывод, перекатки, порты и исключения по сценариям."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FAKES = Path(__file__).with_name('ports_fakes.py')
CASES = json.loads((Path(__file__).parent / 'fixtures/ports/cases.json').read_text())
CONFIG = '''entry:
  host: 192.0.2.1
  password: entry-pass
exits:
  - tag: se
    host: 192.0.2.2
    password: se-pass
  - tag: nl
    host: 192.0.2.3
    password: nl-pass
    hy2_direct_port: 4443
admin:
  user: anysda
'''
HOSTS = {'192.0.2.2': 'se', '192.0.2.3': 'nl'}

# Деплой вокруг проверки: перекатка стадии пишет событие, сбой - по имени журнала.
HARNESS = r'''
set -euo pipefail
C_R='' C_G='' C_Y='' C_B='' C_END='' HOST_TAG=deploy _STAGE_LOG=()
die()  { printf '%b[%s] FATAL:%b %s\n' "$C_R" "${HOST_TAG:-?}" "$C_END" "$*" >&2; exit 1; }
step() { printf '%b==>%b %s\n' "$C_B" "$C_END" "$*"; }
inventory() {
  PYTHONPATH="$DEPLOY_ROOT/vpn2/src" python3 -B "$DEPLOY_ROOT/ansible/inventory.py" "$@"
}
envs_write() {
  local node
  ENVS=$TMP/envs
  install -d -m 700 "$ENVS"
  inventory --env ru > "$ENVS/ru.env"
  for node in $(source "$ENVS/ru.env"; echo "$EXIT_TAGS"); do inventory --env "$node" > "$ENVS/$node.env"; done
}
expand_hosts() { [[ "$1" == ru ]] && echo ru || echo "$PORTS_FOREIGN"; }
run_stage() {
  local me=$BASHPID log
  log=$(basename "$(readlink "/proc/$me/fd/1")")
  echo "rerun $log $1:$2" >>"$PORTS_EVENTS"
  [[ " $PORTS_RERUN_FAIL " != *" $log "* ]] || { echo "stage $1 failed"; exit 1; }
}
'''
FUNCTION = re.search(r'^verify_and_rotate_ports\(\) \{.*?^\}$', (ROOT / 'infra/deploy.sh').read_text(),
                     re.MULTILINE | re.DOTALL)[0]
SCRIPT = HARNESS + FUNCTION + '''
envs_write
verify_and_rotate_ports
printf '%s\\n' "${_STAGE_LOG[@]}" >"$PORTS_STAGELOG"
'''


def run(case, tmp):
    repo, state, bin_dir = tmp / 'repo', tmp / 'state', tmp / 'root/venv/bin'
    for folder in (repo, state, bin_dir):
        folder.mkdir(parents=True)
    (repo / 'config.yaml').write_text(CONFIG)
    (bin_dir / 'ansible').write_text(f'#!/bin/sh\nexec {sys.executable} {FAKES} ansible "$1" "$@"\n')
    (bin_dir / 'python3').write_text(f'#!/bin/sh\ncase " $* " in *" vpn2.cli "*) exec {sys.executable} {FAKES} "$@" ;; esac\n'
                                     f'exec {sys.executable} "$@"\n')
    (bin_dir / 'sleep').write_text('#!/bin/sh\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    scenario = {'hosts': HOSTS, **{key: case[key] for key in ('delay', 'open', 'ssh_fail')}}
    (tmp / 'case.json').write_text(json.dumps(scenario))
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'TMP': str(tmp), 'VPN2_ROOT': str(tmp / 'root'),
           'DEPLOY_ROOT': str(ROOT / 'infra'), 'REPO': str(repo), 'VPN2_CONFIG': str(repo / 'config.yaml'),
           'VPN2_STATE_DIR': str(state), 'PORTS_CASE': str(tmp / 'case.json'), 'PORTS_EVENTS': str(tmp / 'events'),
           'PORTS_FOREIGN': case['foreign'], 'PORTS_STAGELOG': str(tmp / 'stagelog'),
           'PORTS_RERUN_FAIL': ' '.join(case['rerun_fail'])}
    done = subprocess.run(['bash', '-c', SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    excluded = state / 'excluded-exits.txt'

    def text(path):
        return path.read_text().replace(str(tmp), '<tmp>') if path.exists() else None

    return {
        'rc': done.returncode,
        'stdout': done.stdout.replace(str(tmp), '<tmp>'),
        'stderr': done.stderr,
        'events': (text(tmp / 'events') or '').splitlines(),
        'excluded': [text(excluded), oct(excluded.stat().st_mode & 0o777)] if excluded.exists() else None,
        # секунды проверки зависят от машины
        'stagelog': [re.sub(r'\t[0-9ms ]+$', '\tT', line) for line in (text(tmp / 'stagelog') or '').splitlines() if line],
        'config': (repo / 'config.yaml').read_text(),
        'logs': sorted(path.name for path in state.glob('*.log')),
    }


@pytest.mark.parametrize('case', CASES, ids=[case['name'] for case in CASES])
def test_ports_check(case, tmp_path):
    assert run(case, tmp_path) == case['expected']
