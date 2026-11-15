"""Плей из прогретого процесса плеев выходит тем же, что отдельный ansible-playbook с теми же аргументами,
без пустых шапок плеев, чей тег не назван в --tags."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'infra/tests'))
import vpn_roles  # noqa: E402

from vpn2.deploy.plays import Plays  # noqa: E402

CFG = '[defaults]\ninventory = hosts\nroles_path = roles\ngathering = explicit\nretry_files_enabled = False\n'
# Плей служебный и две стадии; шаблоны в when и в выводе берут -e, переменные роли и окружение плея.
DEPLOY = """- name: Служебный
  hosts: all
  tasks:
    - name: Журнал
      ansible.builtin.debug: {msg: "журнал {{ journal }}"}
      tags: [release]
      when: journal | default(true) | bool
"""
SITE = """- name: Первая
  hosts: all
  tags: [one]
  roles: [first]

- name: Вторая
  hosts: all
  tags: [two]
  roles: [second]
"""
FIRST = """- name: Переменная роли и окружение
  ansible.builtin.debug: {msg: "{{ greeting }} {{ lookup('env', 'PLAY_MARK') }}"}
  when: greeting | length > 0
"""


def tree(tmp_path):
    ansible = tmp_path / 'infra/ansible'
    for name, text in (('ansible.cfg', CFG), ('deploy.yml', DEPLOY), ('site.yml', SITE),
                       ('hosts', f'localhost ansible_connection=local ansible_python_interpreter={sys.executable}\n'),
                       ('roles/first/tasks/main.yml', FIRST), ('roles/first/defaults/main.yml', 'greeting: привет\n'),
                       ('roles/second/tasks/main.yml', '- ansible.builtin.fail: {msg: вторая стадия}\n')):
        (ansible / name).parent.mkdir(parents=True, exist_ok=True)
        (ansible / name).write_text(text)
    venv = tmp_path / 'root/venv/bin'
    venv.mkdir(parents=True)
    script = Path(vpn_roles.ansible_playbook())
    (venv / 'ansible-playbook').symlink_to(script)
    (venv / 'python3').write_text(f'#!/bin/sh\nexec {script.read_text().split(chr(10), 1)[0][2:].strip()} "$@"\n')
    (venv / 'python3').chmod(0o755)
    return ansible


def test_play_from_warm_process_matches_separate_ansible_playbook(tmp_path):
    ansible = tree(tmp_path)
    env = {**os.environ, 'ANSIBLE_CONFIG': str(ansible / 'ansible.cfg'), 'PLAY_MARK': 'метка', 'ANSIBLE_NOCOLOR': '1'}
    # Аргументы плея и шапки, которых нет у прогретого: плеи вне --tags; site.yml, где ни один плей
    # не назван, остаётся целым - пустой плейбук ansible отвергает.
    runs = [([ansible / 'deploy.yml', ansible / 'site.yml', '--tags', 'release,one', '-e', 'journal=true'], 'Вторая'),
            ([ansible / 'deploy.yml', ansible / 'site.yml', '--tags', 'two'], 'Первая'),
            ([ansible / 'deploy.yml', ansible / 'site.yml', '--tags', 'release', '-e', 'journal=false'], None)]
    cold = [subprocess.run([tmp_path / 'root/venv/bin/ansible-playbook', *args], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL) for args, _skipped in runs]
    plays = Plays(tmp_path / 'root', env)
    try:
        warm = []
        for number, (args, _skipped) in enumerate(runs):
            with open(tmp_path / f'out{number}', 'w') as out, open(tmp_path / f'err{number}', 'w') as err:
                code = plays.run(list(map(str, args)), env, out, err)
            warm.append((code, (tmp_path / f'out{number}').read_text(), (tmp_path / f'err{number}').read_text()))
    finally:
        plays.close()
    assert [found.returncode for found in cold] == [0, 2, 0]
    assert 'журнал true' in cold[0].stdout and 'привет метка' in cold[0].stdout and 'вторая стадия' in cold[1].stdout
    banner = r'PLAY \[{}\] \*+\n\n'
    assert all(re.search(banner.format(skipped), found.stdout) for found, (_args, skipped) in zip(cold, runs) if skipped)
    assert warm == [(found.returncode, re.sub(banner.format(skipped), '', found.stdout) if skipped else found.stdout,
                     found.stderr) for found, (_args, skipped) in zip(cold, runs)]
    assert plays.proc.returncode == 0


def test_late_code_of_interrupted_play_keeps_reader_alive():
    """Код плея, чьё ожидание прервано (запись уже снята), не роняет читателя: следующий плей получает свой код."""
    import json
    import socket
    import threading

    plays = Plays.__new__(Plays)
    plays.sock, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    plays.lock, plays.waiting = threading.Lock(), {2: {'done': threading.Event()}}
    reader = threading.Thread(target=plays.read, daemon=True)
    reader.start()
    theirs.send(json.dumps({'id': 1, 'code': 143}).encode())
    theirs.send(json.dumps({'id': 2, 'code': 0}).encode())
    assert plays.waiting[2]['done'].wait(5)
    assert plays.waiting[2]['code'] == 0
    assert reader.is_alive()
    theirs.close()
    reader.join(5)
