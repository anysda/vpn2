"""Стадия-плей для проверок deploy.sh без узлов.

Настоящий run_play из infra/lib/ansible-venv.sh зовёт подставной
ansible-playbook. Тот читает заглушки проверки (stubs.sh) и отдаёт им стадию
той же командой, какой её узнают заглушки ssh_exec: /tmp/anysda/<стадия>.sh.
"""
import os
from pathlib import Path


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

# Ставится после функций deploy.sh и библиотеки: venv и колесо экзита без сборки.
PLAY_STUBS = '''
VPN2_ROOT=$PWD
export VPN2_ROOT
: > wheel.whl
ansible_venv_ensure() { :; }
ansible_wheel_build() { :; }
ansible_wheel_path() { echo "$VPN2_ROOT/wheel.whl"; }
'''

STAGE_BODY = 'load_env "$HOST" || exit 1\nssh_exec "/tmp/anysda/$STAGE.sh"\n'


def run_play_source():
    text = (ROOT / 'infra/lib/ansible-venv.sh').read_text()
    return 'run_play() {' + text.split('run_play() {', 1)[1].split('\n}', 1)[0] + '\n}\n'


def site_tree(root, stages, hosts='all'):
    """site.yml с плеями стадий и пакет vpn2 там, где его ищет deploy.sh ($DEPLOY_ROOT/vpn2/src)."""
    root = Path(root)
    (root / 'ansible').mkdir(parents=True, exist_ok=True)
    site = root / 'ansible/site.yml'
    known = site.read_text() if site.exists() else ''
    site.write_text(known + ''.join(f'- hosts: {hosts}\n  tags: [{stage}]\n' for stage in stages))
    package = root / 'vpn2/src'
    if package.is_dir() and not package.is_symlink():
        # Пакет в каталоге проверки подставной: стадии читает настоящий модуль.
        (package / 'vpn2').mkdir(exist_ok=True)
        (package / 'vpn2/stages.py').write_text((ROOT / 'infra/vpn2/src/vpn2/stages.py').read_text())
    elif not package.exists():
        (root / 'vpn2').mkdir(exist_ok=True)
        package.symlink_to(ROOT / 'infra/vpn2/src')


def real_site_tree(root):
    """Настоящий site.yml продукта в каталоге проверки."""
    root = Path(root)
    (root / 'ansible').mkdir(parents=True, exist_ok=True)
    (root / 'ansible/site.yml').write_text((ROOT / 'infra/ansible/site.yml').read_text())
    site_tree(root, [])


def site_play(stage):
    """Плей стадии из site.yml продукта."""
    import yaml
    play, = [play for play in yaml.safe_load((ROOT / 'infra/ansible/site.yml').read_text())
             if play['tags'] == [stage]]
    return play


def play_tree(root, stages, stubs, body=STAGE_BODY):
    """Плеи стадий и подставной ansible-playbook в каталоге проверки."""
    root = Path(root)
    site_tree(root, stages)
    (root / 'venv/bin').mkdir(parents=True, exist_ok=True)
    (root / 'stubs.sh').write_text(stubs)
    playbook = root / 'venv/bin/ansible-playbook'
    # Вызов: ansible-playbook site.yml --limit <узел> --tags <стадия>
    playbook.write_text('#!/bin/bash\ncd "$VPN2_ROOT" || exit 1\nsource ./stubs.sh\n'
                        'STAGE=$5\nHOST=$3\n' + body)
    playbook.chmod(0o755)
