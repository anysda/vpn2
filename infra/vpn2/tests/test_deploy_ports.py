"""Проверка портов hy2 после 20-ru-router внутри деплоя: вывод, перекатки стадий в журналы, порты,
исключения и сводка по сценариям fixtures/ports/cases.json (сеть - ports_fakes)."""
import json
import re
from pathlib import Path

import pytest

import ports_fakes
from vpn2 import config, ports
from vpn2.deploy.run import Deploy

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


@pytest.mark.parametrize('case', CASES, ids=[case['name'] for case in CASES])
def test_ports_check(case, tmp_path, monkeypatch, capfd):
    repo, state = tmp_path / 'repo', tmp_path / 'state'
    repo.mkdir()
    state.mkdir()
    (repo / 'config.yaml').write_text(CONFIG)
    scenario = {'hosts': {'192.0.2.2': 'se', '192.0.2.3': 'nl'}, **{key: case[key] for key in ('delay', 'open', 'ssh_fail')}}
    (tmp_path / 'case.json').write_text(json.dumps(scenario))
    for name, value in (('VPN2_CONFIG', repo / 'config.yaml'), ('PORTS_CASE', tmp_path / 'case.json'),
                        ('PORTS_EVENTS', tmp_path / 'events')):
        monkeypatch.setenv(name, str(value))
    write = config.set_exit_port

    def set_exit_port(path, tag, port):
        ports_fakes.event(f'set {tag} {port}')
        write(path, tag, port)

    monkeypatch.setattr(config, 'STATE_DIR', state)
    monkeypatch.setattr(config, 'set_exit_port', set_exit_port)
    monkeypatch.setattr(ports, 'probe_delay', ports_fakes.delay)
    monkeypatch.setattr(ports, 'udp_ask', ports_fakes.udp_answer)
    monkeypatch.setattr(ports, 'on_exit', ports_fakes.ansible)
    monkeypatch.setattr(ports, 'CLASH_SECRET', tmp_path / 'case.json')
    monkeypatch.setattr(ports.time, 'sleep', lambda _seconds: None)
    ports_fakes.PROBES.clear()

    deploy = Deploy.__new__(Deploy)
    deploy.stage_log, deploy.verify_warnings, deploy.deploy_warnings = [], [], []
    deploy.load_nodes = lambda: None
    deploy.expand = lambda _group: case['foreign'].split()

    def run_stage(stage, group):
        log = Path('/proc/self/fd/1').resolve().name
        ports_fakes.event(f'rerun {log} {stage}:{group}')
        deploy.stage_log.append(f'{stage}\t{group}\t1s')
        if log in case['rerun_fail']:
            print(f'stage {stage} failed')
            raise SystemExit(1)

    deploy.run_stage = run_stage
    code = 0
    try:
        deploy.rotate_ports()
    except SystemExit as error:
        print(error.code, file=__import__('sys').stderr)
        code = 1
    out, err = capfd.readouterr()
    excluded = state / 'excluded-exits.txt'
    events = tmp_path / 'events'
    assert {
        'rc': code,
        'stdout': out.replace(str(tmp_path), '<tmp>'),
        'stderr': err,
        'events': events.read_text().splitlines() if events.exists() else [],
        'excluded': [excluded.read_text(), oct(excluded.stat().st_mode & 0o777)] if excluded.exists() else None,
        'stagelog': [re.sub(r'\t[0-9ms ]+$', '\tT', line) for line in deploy.stage_log],
        'config': (repo / 'config.yaml').read_text(),
        'logs': sorted(path.name for path in state.glob('*.log')),
    } == case['expected']
