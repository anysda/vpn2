"""Run the deployed generators in an isolated directory with fixture secrets."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import vpn_roles

ROOT = Path(os.environ.get('INFRA_TEST_ROOT', Path(__file__).resolve().parents[2]))
SB = os.environ.get('SING_BOX_BIN')
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))

KINDS = ('DIRECT', 'WARP', 'MGMT', 'OBFS')
PLAY = '''- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.include_vars: {root}/infra/ansible/roles/router/defaults/main.yml
    - ansible.builtin.template: {{src: {root}/infra/ansible/roles/router/templates/config-base.json.j2, dest: {out}}}
'''


def upper(tag):
    return tag.upper().replace('-', '_')


def render(env):
    """Шаблон роли router на окружении в виде прежних переменных генератора."""
    tags = env['EXIT_TAGS'].split()
    secret = {f'{upper(t)}_{k}': env[f'{upper(t)}_{k}'] for t in tags for k in KINDS if f'{upper(t)}_{k}' in env}
    reality = {t: {key: env[f'{upper(t)}_REALITY_{name}'] for key, name in
                   (('PUB', 'PUBKEY'), ('SID', 'SHORT_ID'), ('SNI', 'SNI'), ('PORT', 'PORT'))
                   if f'{upper(t)}_REALITY_{name}' in env} for t in tags if f'{upper(t)}_REALITY_PUBKEY' in env}
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        links = Path(exit_cert()).parent / 'pinned'
        links.mkdir(exist_ok=True)
        for tag in tags:
            pem = env.get(f'{upper(tag)}_HY2_CERT_PATH')
            (links / f'{tag}.pem').unlink(missing_ok=True)
            if pem:
                (links / f'{tag}.pem').symlink_to(pem)
        variables = {'vpn2_env': {k: v for k, v in env.items() if k not in secret}, 'router_secret': secret,
                     'router_reality': reality, 'router_wan': env.get('WG_OUT_IFACE', 'lo'),
                     'router_clash_secret': env.get('RU_CLASH_SECRET', ''), 'router_certs': str(links),
                     'router_infra': str(ROOT / 'infra')}
        if env.get('LANE_SUBNETS'):
            pools = json.loads((ROOT / 'app/shared/vpn-pools.json').read_text())
            pools['wg']['subnets'] += env['LANE_SUBNETS'].split()
            variables['router_pools'] = pools
        (work / 'vars.json').write_text(json.dumps(variables))
        (work / 'play.yml').write_text(PLAY.format(root=ROOT, out=work / 'out.json'))
        done = subprocess.run([vpn_roles.ansible_playbook(), '-i', 'localhost,', str(work / 'play.yml'),
                               '-e', '@' + str(work / 'vars.json')], text=True, capture_output=True)
        out = (work / 'out.json').read_text() if (work / 'out.json').exists() else ''
        return subprocess.CompletedProcess(done.args, done.returncode, out, done.stdout + done.stderr)


def stop(process):
    process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


_CERT_DIR = None


def exit_cert():
    """Self-signed PEM как у экзита: генератор требует закреплённый сертификат."""
    global _CERT_DIR
    if _CERT_DIR is None:
        _CERT_DIR = tempfile.TemporaryDirectory()
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=exit',
                        '-keyout', os.path.join(_CERT_DIR.name, 'exit.key'),
                        '-out', os.path.join(_CERT_DIR.name, 'exit.pem')],
                       check=True, capture_output=True)
    return os.path.join(_CERT_DIR.name, 'exit.pem')


def generate(**overrides):
    result = run_generator(**overrides)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            result.stdout, result.stderr)
    return result.stdout


def run_generator(**overrides):
    env = {
        'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '1443', 'HY2_WARP_PORT': '2443',
        'HY2_MGMT_PORT': '3443', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test',
        'MGMT_IP': '127.0.0.1', 'LANE_SUBNETS': '192.0.2.0/29', 'LANES': '4',
    }
    for tag, suffix in [('AA', '2'), ('BB', '3')]:
        env.update({f'DOMAIN_{tag}': f'{tag.lower()}.example.test',
                    f'MGMT_IP_{tag}': f'192.0.2.{suffix}',
                    f'{tag}_HY2_CERT_PATH': exit_cert()})
        for key in ['DIRECT', 'WARP', 'MGMT', 'OBFS']:
            env[f'{tag}_{key}'] = 'test-password-0123456789'
    env.update(overrides)
    # Любой набор экзитов (стенд панели берёт свой) получает закреплённый сертификат.
    for tag in env['EXIT_TAGS'].split():
        env.setdefault(f'{tag.upper()}_HY2_CERT_PATH', exit_cert())
    return render(env)


def exit_secrets(tags, password='test-password-0123456789'):
    """Секреты экзитов в том виде, в каком деплой кладёт их в foreign-secrets.env."""
    return {f'{upper(tag)}_{kind}': password for tag in tags for kind in KINDS}


def role_tasks():
    import yaml
    return yaml.safe_load((ROOT / 'infra/ansible/roles/router/tasks/main.yml').read_text())


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
    source = (ROOT / 'infra/vpn2/src/vpn2/router/apply_routes.py').read_text()
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


def client_block_rules():
    """Блок клиентских назначений из сгенерированного конфига: три правила вокруг ip_is_private."""
    rules = json.loads(generate())['route']['rules']
    start = next(i for i, rule in enumerate(rules) if rule.get('ip_is_private')) - 1
    return rules[start:start + 3]
