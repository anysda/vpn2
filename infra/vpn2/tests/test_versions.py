"""vpn2.config.versions: infra/versions.yml против пинов в стадиях и конфига ansible."""
import re
from pathlib import Path

import pytest
import yaml

from vpn2 import config

INFRA = Path(__file__).resolve().parents[2]
VERSIONS = INFRA / 'versions.yml'

# ключ -> (файл со стадией, имя переменной версии)
STAGE_PINS = {
    'sing_box': ('lib/install-sing-box.sh', 'SB_VER'),
    'wgcf': ('scripts/10-foreign.sh', 'WGCF_VER'),
    'adguard': ('scripts/22-adguard.sh', 'AGH_VER'),
    'caddy': ('lib/anysda-svc.sh', 'CADDY_VER'),
    'victoriametrics': ('scripts/25-monitoring.sh', 'VM_VER'),
}


def stage_version(relpath, var):
    text = (INFRA / relpath).read_text(encoding='utf-8')
    found = re.search(rf"^(?:readonly )?{var}='([^']+)'", text, re.M)
    assert found, f'{relpath}: нет присваивания {var}'
    return found.group(1)


def check_stages(pins):
    for key, (relpath, var) in STAGE_PINS.items():
        assert pins[key] == stage_version(relpath, var), key


def test_keys():
    assert set(config.versions(VERSIONS)) == {'ansible_core', 'ansible_posix', *STAGE_PINS}


def test_matches_stages():
    check_stages(config.versions(VERSIONS))


def test_matches_stage_detects_drift(tmp_path):
    drifted = tmp_path / 'versions.yml'
    drifted.write_text(VERSIONS.read_text(encoding='utf-8').replace('2.11.9', '2.11.8'), encoding='utf-8')
    with pytest.raises(AssertionError):
        check_stages(config.versions(drifted))


def test_matches_constraints():
    first = (INFRA / 'ansible/constraints.txt').read_text(encoding='utf-8').splitlines()[0]
    assert first == f"ansible-core=={config.versions(VERSIONS)['ansible_core']}"


def test_matches_requirements():
    reqs = yaml.safe_load((INFRA / 'ansible/requirements.yml').read_text(encoding='utf-8'))
    posix = [c for c in reqs['collections'] if c['name'] == 'ansible.posix']
    assert [c['version'] for c in posix] == [f"=={config.versions(VERSIONS)['ansible_posix']}"]


@pytest.mark.parametrize('text', ['- a\n- b\n', 'caddy: 2\n', 'caddy: {a: b}\n', ''])
def test_bad_file(tmp_path, text):
    bad = tmp_path / 'versions.yml'
    bad.write_text(text, encoding='utf-8')
    with pytest.raises(config.ConfigError):
        config.versions(bad)
