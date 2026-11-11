"""Hostvars инвентаря ansible: vpn2_env равен env-файлам config2env.py, секреты в логи не идут."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from nodevars_support import ROOT, differences, layout, old_env
from test_nodevars import EXITS3, FULL, MATRIX, MINIMAL
from vpn2 import config, nodevars

SCRIPT = ROOT / 'infra/ansible/inventory.py'
spec = importlib.util.spec_from_file_location('vpn2_inventory', SCRIPT)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)

# Имена, похожие на секрет, но открытые: ключ публичный, переключатель и подсказка.
PUBLIC = {'ADMIN_SSH_PUBKEY', 'SSO_PASSWORD_LOGIN'}
JINJA = MINIMAL.replace('password: p\n', 'password: "a{{ 1 + 1 }}b{% x %}"\n', 1)


def plain(value):
    """Снять пометку «не шаблонизировать»: содержимое то же."""
    if isinstance(value, dict):
        return value['__ansible_unsafe'] if set(value) == {'__ansible_unsafe'} else {k: plain(v) for k, v in value.items()}
    return [plain(item) for item in value] if isinstance(value, list) else value


def build(text, tmp, *, environ=None, state=None, legacy=None, development=False, **options):
    repo, state_dir = layout(tmp, text, state=state, legacy=legacy)
    environ = dict(environ or {}, **({'VPN2_DEVELOPMENT': 'true'} if development else {}))
    return repo, state_dir, inventory.inventory(config.loads(text), state_dir, repo_root=repo, environ=environ, **options)


def envs(result):
    return {tag: plain(host['vpn2_env']) for tag, host in result['_meta']['hostvars'].items() if 'vpn2_env' in host}


@pytest.mark.parametrize('name', sorted(MATRIX))
def test_hostvars_equal_config2env(name):
    text, options = MATRIX[name]
    with tempfile.TemporaryDirectory() as tmp:
        repo, state_dir, result = build(text, tmp, **options)
        old = old_env(repo, state_dir, development=options.get('development', False))
    for env in old.values():
        # Настройка запуска: bash держит её в окружении процесса, а не в env-файлах.
        env.update({'VPN2_DEVELOPMENT': 'true'} if options.get('development') else {})
    assert differences(old, envs(result)) == []
    for host in result['_meta']['hostvars'].values():
        assert all(isinstance(value, str) for value in plain(host.get('vpn2_env', {})).values())


def test_typed_vars():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, result = build(EXITS3, tmp)
    hosts = result['_meta']['hostvars']
    assert hosts['ru']['vpn2_node'] == {'tag': 'ru', 'role': 'entry', 'host': '198.18.0.1', 'mgmt_ip': '10.99.0.1'}
    assert hosts['de-2']['vpn2_node'] == {'tag': 'de-2', 'role': 'exit', 'host': 'de.example.org', 'mgmt_ip': '10.99.0.3'}
    assert hosts['ru']['vpn2_exits'] == hosts['us']['vpn2_exits']
    assert hosts['ru']['vpn2_exits'][:2] == [
        {'tag': 'nl', 'env_tag': 'NL', 'host': '198.18.0.2', 'mgmt_ip': '10.99.0.2', 'hy2_direct_port': 443,
         'hy2_up_mbps': None, 'hy2_down_mbps': None, 'hy2_insecure': False},
        {'tag': 'de-2', 'env_tag': 'DE_2', 'host': 'de.example.org', 'mgmt_ip': '10.99.0.3', 'hy2_direct_port': 4443,
         'hy2_up_mbps': 100, 'hy2_down_mbps': 200, 'hy2_insecure': True}]


def test_run_settings_pass_through_and_config_wins():
    environ = {'VPN2_INFRA': '/srv/infra', 'YT_ROUTE': 'direct', 'PANEL_IMAGE': 'panel:env', 'NL_HY2_MGMT_PORT': '9444',
               'HOME': '/root', 'VAULT_TOKEN': 'x'}
    with tempfile.TemporaryDirectory() as tmp:
        _, _, result = build(MINIMAL, tmp, environ=environ)
    for tag in ('ru', 'nl'):
        env = envs(result)[tag]
        assert (env['VPN2_INFRA'], env['PANEL_IMAGE'], env['NL_HY2_MGMT_PORT']) == ('/srv/infra', 'panel:env', '9444')
        assert 'HOME' not in env and 'VAULT_TOKEN' not in env
    # ru.env задаёт YT_ROUTE сам; экзиту он, как и в bash, достаётся из окружения запуска.
    assert (envs(result)['ru']['YT_ROUTE'], envs(result)['nl']['YT_ROUTE']) == ('off', 'direct')


def test_preserved_admin_password_is_absent():
    text = MINIMAL + 'admin:\n  password: "s3"\n'
    with tempfile.TemporaryDirectory() as tmp:
        _, _, kept = build(text, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _, _, dropped = build(text, tmp, environ={'VPN2_PRESERVE_ADMIN': '1'})
    assert envs(kept)['ru']['ADMIN_PASSWORD'] == 's3'
    assert 'ADMIN_PASSWORD' not in envs(dropped)['ru']


def test_rejected_config_keeps_hosts_and_names_the_key():
    text = MINIMAL + 'youtube:\n  route: "S3CRET"\n'
    with tempfile.TemporaryDirectory() as tmp:
        _, _, result = build(text, tmp)
    assert result['all']['vars'] == {'vpn2_env_error': 'youtube.route'}
    assert result['exits']['hosts'] == ['nl'] and envs(result) == {}
    assert 'S3CRET' not in json.dumps(result)


def test_every_secret_looking_name_is_classified():
    names = set(nodevars.CONFIG_NAMES)
    with tempfile.TemporaryDirectory() as tmp:
        _, _, result = build(FULL, tmp, development=True)
    names |= {name for env in envs(result).values() for name in env}
    suspicious = {name for name in names if re.search(r'PASS|SECRET|TOKEN|KEY', name)}
    assert suspicious - PUBLIC == nodevars.SECRET_NAMES


def test_hidden_secrets_leave_no_value_in_output():
    secrets = ('entry-S3CRET', 'exit-S3CRET', 'admin-S3CRET', '77:tok-S3CRET', 'sso-S3CRET', 'pass-S3CRET',
               'ak-S3CRET', 'sk-S3CRET')
    text = (f'entry:\n  host: 198.18.0.1\n  password: "{secrets[0]}"\nexits:\n  - tag: nl\n    host: 198.18.0.2\n'
            f'    password: "{secrets[1]}"\nadmin:\n  password: "{secrets[2]}"\n'
            f'telegram:\n  bot_token: "{secrets[3]}"\n  chat_id: "5"\n'
            f'sso:\n  enabled: true\n  base_url: https://sso.example.org\n  client_secret: "{secrets[4]}"\n'
            f'backup:\n  enabled: true\n  backend: s3\n  passphrase: "{secrets[5]}"\n  s3:\n    bucket: b\n'
            f'    access_key: "{secrets[6]}"\n    secret_key: "{secrets[7]}"\n')
    with tempfile.TemporaryDirectory() as tmp:
        _, _, shown = build(text, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _, _, hidden = build(text, tmp, hide_secrets=True)
    assert all(secret in json.dumps(shown) for secret in secrets), 'положительный контроль: секреты в выводе есть'
    assert 'S3CRET' not in json.dumps(hidden)
    assert envs(hidden)['ru']['SSH_PASS'] == inventory.HIDDEN == hidden['_meta']['hostvars']['nl']['ansible_password']


def run_script(tmp, text, **extra):
    repo, state_dir = layout(tmp, text)
    env = dict(os.environ, VPN2_CONFIG=str(repo / 'config.yaml'), VPN2_STATE_DIR=str(state_dir),
               PYTHONPATH=str(ROOT / 'infra/vpn2/src'), **extra)
    env.pop('VPN2_DEVELOPMENT', None)
    return env


def test_script_hides_secrets_on_request():
    with tempfile.TemporaryDirectory() as tmp:
        env = run_script(tmp, MINIMAL.replace('password: p\n', 'password: entry-S3CRET\n', 1))
        outputs = [subprocess.run([sys.executable, str(SCRIPT), '--list'], env=dict(env, **mode), check=True,
                                  capture_output=True, text=True, timeout=30).stdout
                   for mode in ({}, {'VPN2_INVENTORY_SECRETS': 'hide'})]
    assert 'entry-S3CRET' in outputs[0] and 'entry-S3CRET' not in outputs[1]


@pytest.mark.skipif(not shutil.which('ansible-inventory'), reason='нужен ansible')
def test_ansible_reads_values_verbatim_and_inventory_listing_is_masked():
    with tempfile.TemporaryDirectory() as tmp:
        env = run_script(tmp, JINJA, ANSIBLE_LOCALHOST_WARNING='False')
        launcher = Path(tmp) / 'inventory.sh'
        launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {SCRIPT} "$@"\n', encoding='utf-8')
        launcher.chmod(0o755)
        message = '{{ ansible_password }}|{{ vpn2_env.SSH_PASS }}|{{ vpn2_env.NOPE | default("") }}|{{ vpn2_node.role }}'
        play = subprocess.run(['ansible', '-i', str(launcher), 'ru', '-c', 'local', '-m', 'ansible.builtin.debug',
                               '-a', 'msg=' + message], env=env, capture_output=True, text=True, timeout=120)
        listing = subprocess.run(['ansible-inventory', '-i', str(launcher), '--list'], env=env,
                                 capture_output=True, text=True, timeout=120)
    assert 'a{{ 1 + 1 }}b{% x %}|a{{ 1 + 1 }}b{% x %}||entry' in play.stdout, play.stdout + play.stderr
    assert listing.returncode == 0 and '"SSH_PASS": "***"' in listing.stdout, listing.stdout + listing.stderr
    assert '1 + 1' not in listing.stdout
