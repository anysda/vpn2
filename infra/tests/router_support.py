"""Run the deployed generators in an isolated directory with fixture secrets."""

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(os.environ.get('INFRA_TEST_ROOT', Path(__file__).resolve().parents[2]))
SB = os.environ.get('SING_BOX_BIN')


def stop(process):
    process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def generate(**overrides):
    env = {
        'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '1443', 'HY2_WARP_PORT': '2443',
        'HY2_MGMT_PORT': '3443', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test',
        'MGMT_IP': '127.0.0.1', 'LANE_SUBNETS': '192.0.2.0/29', 'LANES': '4',
    }
    for tag, suffix in [('AA', '2'), ('BB', '3')]:
        env.update({f'DOMAIN_{tag}': f'{tag.lower()}.example.test',
                    f'MGMT_IP_{tag}': f'192.0.2.{suffix}'})
        for key in ['DIRECT', 'WARP', 'MGMT', 'OBFS']:
            env[f'{tag}_{key}'] = 'test-password-0123456789'
    env.update(overrides)
    return subprocess.run([sys.executable, str(ROOT / 'infra/lib/gen-router-config.py')],
                          env=env, text=True, capture_output=True, check=True).stdout


def stage_source():
    return (ROOT / 'infra/scripts/20-ru-router.sh').read_text()


def prepare(directory, **overrides):
    cfg = json.loads(generate(**overrides))
    for item in cfg['route']['rule_set']:
        path = directory / (item['tag'] + '.json')
        path.write_text(json.dumps({'version': 2, 'rules': []}))
        item.update(format='source', path=str(path))
    cfg['experimental']['cache_file']['path'] = str(directory / 'cache.db')
    (directory / 'base.json').write_text(json.dumps(cfg))
    (directory / 'manual.json').write_text('[]')
    (directory / 'prefs.json').write_text('{}')
    source = stage_source().split("<< 'PYEOF'\n", 1)[1].split('\nPYEOF', 1)[0]
    replacements = {
        '/etc/anysda/manual-routes.json': directory / 'manual.json',
        '/etc/anysda/client-prefs.json': directory / 'prefs.json',
        '/etc/anysda/route-plan.json': directory / 'plan.json',
        '/etc/sing-box/config-base.json': directory / 'base.json',
        '/etc/sing-box/config.json': directory / 'config.json',
        '/etc/sing-box/manual-routes': directory / 'rules',
        '/etc/sing-box/client-prefs': directory / 'prefs',
        '/usr/local/bin/sing-box': SB,
    }
    for old, new in replacements.items():
        source = source.replace(old, str(new))
    script = directory / 'apply.py'
    script.write_text(source)
    return script


def apply(script):
    return subprocess.run([sys.executable, str(script), '--no-restart'],
                          capture_output=True, text=True)


def snapshot(directory):
    return {str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.rglob('*.json')
            if path.name not in ('manual.json', 'prefs.json')}
