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
    'wgcf': ('ansible/roles/exit/defaults/main.yml', 'exit_wgcf_version'),
    'caddy': ('lib/anysda-svc.sh', 'CADDY_VER'),
}


def stage_version(relpath, var):
    text = (INFRA / relpath).read_text(encoding='utf-8')
    found = re.search(rf"^(?:readonly )?{var}(?:=|: )'([^']+)'", text, re.M)
    assert found, f'{relpath}: нет присваивания {var}'
    return found.group(1)


def check_stages(pins):
    for key, (relpath, var) in STAGE_PINS.items():
        assert pins[key] == stage_version(relpath, var), key


# Пины, записанные в стадиях не переменной в кавычках: ключ -> (файл, строка
# с версией). Пока стадия жива, её литерал обязан совпадать с versions.yml;
# роль, пришедшая на смену, читает versions.yml сама, и строка отсюда уходит.
STAGE_LINES = {
    'sing_box_sha256_amd64': ('lib/install-sing-box.sh', "readonly SB_SHA256_AMD64='{v}'"),
    'sing_box_sha256_arm64': ('lib/install-sing-box.sh', "readonly SB_SHA256_ARM64='{v}'"),
    'node_exporter': ('ansible/roles/base/defaults/main.yml', "base_node_exporter_version: '{v}'"),
    'adguard': ('vpn2/src/vpn2/adguard/stage.py', "AGH_VER = '{v}'\n"),
    'yt_node': ('vpn2/src/vpn2/zapret/stage.py', "NODE_VER = '{v}'\n"),
    'yt_node_sha256': ('vpn2/src/vpn2/zapret/stage.py', "NODE_SHA256 = '{v}'\n"),
    'yt_pot': ('lib/install-yt-pot.sh', 'pot_version={v}\n'),
    'yt_pot_sha256': ('lib/install-yt-pot.sh', '{v}'),
    'zapret2': ('vpn2/src/vpn2/zapret/stage.py', "ZAPRET_REF = '{v}'\n"),
}


def check_lines(pins):
    for key, (relpath, line) in STAGE_LINES.items():
        path = INFRA / relpath
        if path.exists():
            assert line.format(v=pins[key]) in path.read_text(encoding='utf-8'), key


def test_keys():
    assert set(config.versions(VERSIONS)) == {'ansible_core', 'ansible_posix', 'aws_cli', 'victoriametrics', *STAGE_PINS,
                                             *STAGE_LINES}


def test_matches_stages():
    check_stages(config.versions(VERSIONS))
    check_lines(config.versions(VERSIONS))


def test_release_reader_agrees():
    """Выпуск читает файл без PyYAML (до venv) и видит то же, что vpn2.config."""
    from vpn2 import release
    assert release.versions(VERSIONS) == config.versions(VERSIONS)


def test_no_pins_outside_versions():
    """Сборщик манифеста не держит своих версий: только ключи versions.yml."""
    text = (INFRA / 'vpn2/src/vpn2/release/build.py').read_text(encoding='utf-8')
    pins = config.versions(VERSIONS)
    assert [key for key, value in pins.items() if value in text] == []
    assert not re.search(r"['\"]v?\d+\.\d+\.\d+['\"]", text)


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
