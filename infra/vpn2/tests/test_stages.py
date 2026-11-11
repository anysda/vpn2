"""site.yml - единственный список стадий: имена, узлы и порядок полного деплоя."""
import subprocess
import sys

import pytest
import yaml

from nodevars_support import ROOT
from vpn2 import stages

SITE = ROOT / 'infra/ansible/site.yml'
# Порядок полного деплоя (do_all в infra/deploy.sh) и группа узлов каждой стадии.
PIPELINE = [
    ('00-bootstrap', 'all'), ('05-mgmt-mesh', 'all'), ('10-foreign', 'exits'),
    ('28-wireguard', 'entry'), ('29-openvpn', 'entry'), ('19-yt-zapret', 'entry'),
    ('20-ru-router', 'entry'), ('21-failover-watchdog', 'entry'), ('22-adguard', 'entry'),
    ('23-emergency', 'all'), ('25-monitoring', 'entry'), ('26-backup', 'entry'), ('27-ikev2', 'entry'),
    ('29-control', 'entry'), ('35-telegram', 'entry'), ('30-frontend', 'entry'), ('99-verify', 'all'),
]
# Роли плеев до сведения в site.yml: стадия -> роли и их переменные.
ROLES = {
    '00-bootstrap': ['base'], '05-mgmt-mesh': ['mgmt'], '10-foreign': ['exit'], '19-yt-zapret': ['zapret'],
    '20-ru-router': ['router'], '21-failover-watchdog': ['failover'], '22-adguard': ['adguard'],
    '23-emergency': ['emergency'], '25-monitoring': [{'role': 'containers', 'containers_service': 'monitoring'}],
    '26-backup': ['backup'], '27-ikev2': ['ikev2'], '28-wireguard': ['wireguard'], '29-control': ['control'],
    '29-openvpn': ['openvpn'], '30-frontend': [{'role': 'containers', 'containers_service': 'panel'}],
    '35-telegram': [{'role': 'containers', 'containers_service': 'telegram'}], '99-verify': ['verify'],
}
SERIAL = {'10-foreign': 1, '23-emergency': 1}


def test_site_lists_stages_in_pipeline_order():
    assert [tuple(stage) for stage in stages.load(SITE)] == PIPELINE


def test_parser_reads_site_like_yaml():
    plays = yaml.safe_load(SITE.read_text(encoding='utf-8'))
    assert [(play['tags'], play['hosts']) for play in plays] == [([name], hosts) for name, hosts in PIPELINE]
    assert {play['tags'][0]: play['roles'] for play in plays} == ROLES
    assert {play['tags'][0]: play['serial'] for play in plays if 'serial' in play} == SERIAL
    # Кроме перечисленного в плеях ничего нет: порядок и выбор узлов задаёт только site.yml.
    assert {key for play in plays for key in play} == {'name', 'hosts', 'tags', 'roles', 'serial'}


def test_one_play_file_only():
    assert not (ROOT / 'infra/ansible/plays').exists()


@pytest.mark.parametrize('stage, group, kept', [
    ('00-bootstrap', ['ru', 'nl', 'se'], ['ru', 'nl', 'se']),
    ('30-frontend', ['ru', 'nl', 'se'], ['ru']),
    ('30-frontend', ['ru'], ['ru']),
    ('10-foreign', ['ru', 'nl', 'se'], ['nl', 'se']),
    ('10-foreign', ['se'], ['se']),
    ('23-emergency', ['nl'], ['nl']),
    ('99-verify', [], []),
])
def test_nodes_of_stage(stage, group, kept):
    assert stages.nodes(stages.load(SITE), stage, group, refuse_empty=True) == kept


@pytest.mark.parametrize('stage, group, words', [
    ('30-frontend', ['se'], '30-frontend - стадия entry, на экзитах её нет: группа ru или all'),
    ('22-adguard', ['nl', 'se'], '22-adguard - стадия entry, на экзитах её нет: группа ru или all'),
    ('10-foreign', ['ru'], '10-foreign - стадия экзитов, на entry её нет: группа foreign, all или тег экзита'),
])
def test_stage_without_nodes_in_group_is_refused(stage, group, words):
    with pytest.raises(stages.StageError) as refusal:
        stages.nodes(stages.load(SITE), stage, group, refuse_empty=True)
    assert str(refusal.value) == words
    assert stages.nodes(stages.load(SITE), stage, group) == []


def test_unknown_stage():
    with pytest.raises(stages.UnknownStage) as refusal:
        stages.nodes(stages.load(SITE), '30-frontnd', ['ru'])
    assert str(refusal.value) == 'нет такой стадии: 30-frontnd (список: ./deploy.sh --help)'


def cli(*args):
    return subprocess.run([sys.executable, '-B', '-m', 'vpn2.stages', *args], text=True, capture_output=True,
                          env={'PYTHONPATH': str(ROOT / 'infra/vpn2/src'), 'PATH': '/usr/bin:/bin'})


def test_cli_codes_and_output(tmp_path):
    assert cli('nodes', '30-frontend', 'ru', 'se').stdout == 'ru\n'
    assert cli('nodes', '--refuse-empty', '30-frontend', 'se').returncode == 4
    assert cli('nodes', '30-frontend', 'se').stdout == '\n'
    unknown = cli('nodes', 'nosuch', 'ru')
    assert (unknown.returncode, unknown.stdout) == (3, '')
    assert unknown.stderr == 'нет такой стадии: nosuch (список: ./deploy.sh --help)\n'
    assert cli('list').stdout.splitlines()[0] == '00-bootstrap all'
    site = tmp_path / 'site.yml'
    site.write_text('- hosts: all\n  tags: [90-probe-noop]\n')
    assert cli('nodes', '--site', str(site), '90-probe-noop', 'ru', 'nl').stdout == 'ru nl\n'
    assert cli('bogus').returncode == 2


@pytest.mark.parametrize('text', [
    '- hosts: all\n', '- hosts: nowhere\n  tags: [x]\n', '  hosts: all\n',
    '- hosts: all\n  tags: [a]\n- hosts: all\n  tags: [a]\n', '- hosts: all\n  tags: [a, b]\n',
])
def test_broken_site_is_an_error(text):
    with pytest.raises(ValueError):
        stages.parse(text)
