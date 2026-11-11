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


def play_tree(root, stages, stubs, body=STAGE_BODY):
    """Плеи стадий и подставной ansible-playbook в каталоге проверки."""
    root = Path(root)
    (root / 'ansible/plays').mkdir(parents=True, exist_ok=True)
    for stage in stages:
        (root / 'ansible/plays' / f'{stage}.yml').write_text('- hosts: all\n')
    (root / 'venv/bin').mkdir(parents=True, exist_ok=True)
    (root / 'stubs.sh').write_text(stubs)
    playbook = root / 'venv/bin/ansible-playbook'
    # Вызов: ansible-playbook plays/<стадия>.yml --limit <узел> --tags <стадия>
    playbook.write_text('#!/bin/bash\ncd "$VPN2_ROOT" || exit 1\nsource ./stubs.sh\n'
                        'STAGE=$(basename "$1" .yml)\nHOST=$3\n' + body)
    playbook.chmod(0o755)
