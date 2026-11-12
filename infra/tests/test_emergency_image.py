"""The emergency front ships as a registry image and is installed in standby.

The role's static step runs for real against a fake docker; the rest of the
role is read as tasks.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

import test_release


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
ROLE = ROOT / 'infra/ansible/roles/emergency'

# State in $FAKE/state.json: registry tags, local images, containers, packages.
DOCKER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

fake = Path(os.environ['FAKE'])
state_file = fake / 'state.json'
state = json.loads(state_file.read_text())
args = sys.argv[1:]
with open(fake / 'calls.log', 'a') as log:
    log.write('docker ' + ' '.join(args) + '\n')


def save():
    state_file.write_text(json.dumps(state))


def resolve(ref):
    if ref in state['images']:
        return ref
    return next((i for i, image in state['images'].items() if ref in image['tags']), None)


if args[0] == 'pull':
    image = state['registry'].get(args[1])
    if image is None or os.environ.get('FAKE_OFFLINE') == '1':
        sys.exit(1)
    for other in state['images'].values():
        other['tags'] = [t for t in other['tags'] if t != args[1]]
    state['images'].setdefault(image, {'tags': [], 'labels': {'vpn2.component': 'emergency'}})
    state['images'][image]['tags'].append(args[1])
elif args[:2] == ['image', 'inspect']:
    image = resolve(args[-1])
    if image is None:
        sys.exit(1)
    print(image)
elif args[0] == 'inspect':
    if args[-1] not in state['containers']:
        sys.exit(1)
    print(state['containers'][args[-1]])
elif args[:2] == ['rm', '-f']:
    if state['containers'].pop(args[2], None) is None:
        sys.exit(1)
elif args[0] == 'create':
    name = args[args.index('--name') + 1]
    if name in state['containers'] or '--network' not in args or args[args.index('--network') + 1] != 'none':
        sys.exit(1)
    state['containers'][name] = resolve(args[-1])
elif args[0] == 'cp':
    name, path = args[1].split(':', 1)
    assert path.endswith('/opt/vpn2-emergency/.'), path
    source = fake / 'image' / state['containers'][name].split(':')[1]
    shutil.copytree(source, args[2], dirs_exist_ok=True)
elif args[0] == 'images':
    listing = [a for a in args[1:] if a != '--all']
    assert listing == ['--quiet', '--no-trunc', '--filter', 'label=vpn2.component=emergency'], args
    # Like Docker 29: an image whose tag moved to a newer pull is listed only with --all.
    for image, data in state['images'].items():
        if data['labels'].get('vpn2.component') == 'emergency' and (data['tags'] or '--all' in args):
            print(image)
elif args[:3] == ['image', 'rm', '-f']:
    if args[3] in state['containers'].values():
        sys.exit(1)
    del state['images'][args[3]]
else:
    sys.exit('unexpected docker ' + ' '.join(args))
save()
'''


class EmergencyImageTests(unittest.TestCase):
    def test_ci_builds_emergency_image_next_to_panel(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/images.yml').read_text())
        build = workflow['jobs']['build']
        entries = {item['name']: item for item in build['strategy']['matrix']['include']}
        self.assertIn('emergency', entries, 'CI does not publish the emergency front')
        entry = entries['emergency']
        self.assertEqual(entries['panel']['context'], 'app')
        dockerfile = ROOT / entry['file']
        self.assertTrue(dockerfile.is_file())
        steps = {step.get('name', step.get('id')): step for step in build['steps']}
        meta = steps['meta']['with']['images']
        self.assertEqual(meta, 'ghcr.io/${{ github.repository }}/${{ matrix.name }}')
        for name in ('build', 'push'):
            self.assertEqual(steps[name]['with']['file'], '${{ matrix.file }}')
            self.assertIn('VPN2_VERSION=${{ steps.meta.outputs.version }}', steps[name]['with']['build-args'])
        self.assertIn('smoke emergency', steps)
        self.assertIn('/instructions', steps['smoke emergency']['run'])

    def test_image_carries_only_built_static(self):
        text = (ROOT / 'infra/emergency/Dockerfile').read_text()
        self.assertIn('RUN pnpm emergency:build', text)
        carrier = text.rsplit('\nFROM ', 1)[1]
        self.assertTrue(carrier.startswith('scratch'), 'carrier needs no runtime of its own')
        for needle in ('vpn2_emergency/static /opt/vpn2-emergency/static\n', 'image.json',
                       'LABEL vpn2.component=emergency'):
            self.assertIn(needle, carrier)
        # Агент - пакет vpn2 из venv хоста, юниты едут с деревом деплоя: в образе их нет.
        for needle in ('infra/vpn2', 'install-hy2.sh', '.service'):
            self.assertNotIn(needle, carrier)
        ignore = (ROOT / 'infra/emergency/Dockerfile.dockerignore').read_text().splitlines()
        for needle in ('*', '!app', '**/__pycache__', 'app/node_modules',
                       'infra/emergency/vpn2_emergency/static', '**/.env'):
            self.assertIn(needle, ignore)
        self.assertNotIn('!infra/vpn2/src/vpn2', ignore)

    def test_release_manifest_pins_emergency_image(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = test_release.ReleaseTests().build_manifest(Path(directory), migrations_after_tag=0)
        self.assertEqual(sorted(manifest['images']), ['emergency', 'panel', 'tgbot'])
        result = subprocess.run([sys.executable, '-m', 'vpn2.release', '--manifest',
                                 '/nonexistent/release-manifest.json', 'image', 'emergency', '--offline'],
                                env=dict(os.environ, PYTHONPATH=str(ROOT / 'infra/vpn2/src')),
                                capture_output=True, text=True)
        self.assertNotIn('invalid choice', result.stderr)
        self.assertIn('release manifest not found', result.stderr)

    def test_activation_code_downloads_and_installs_nothing(self):
        for path in (ROOT / 'infra/vpn2/src/vpn2/emergency').glob('*.py'):
            text = path.read_text()
            for needle in ('apt-get', 'apt install', 'dpkg -i', 'pip install', 'docker ', 'ghcr.io'):
                self.assertNotIn(needle, text, f'{path.name}: {needle}')

    # --- шаг статики роли на поддельном Docker -------------------------------

    def host(self, directory):
        fake = Path(directory)
        (fake / 'bin').mkdir()
        (fake / 'bin/docker').write_text(DOCKER)
        (fake / 'bin/docker').chmod(0o755)
        (fake / 'venv/bin').mkdir(parents=True)
        (fake / 'venv/bin/python').write_text(
            f'#!/bin/sh\nPYTHONPATH={ROOT / "infra/vpn2/src"} exec {sys.executable} "$@"\n')
        (fake / 'venv/bin/python').chmod(0o755)
        (fake / 'home').mkdir()
        (fake / 'state.json').write_text(json.dumps({'registry': {}, 'images': {}, 'containers': {}}))
        return fake

    def static(self, fake, *, image, offline=False, static='static'):
        """Шаг статики роли с образом `image` под тегом dev в реестре."""
        state = json.loads((fake / 'state.json').read_text())
        state['registry']['ghcr.io/anysda/vpn2/emergency:dev'] = image
        (fake / 'state.json').write_text(json.dumps(state))
        version = image.split(':')[1]
        layer = fake / 'image' / version
        if not layer.exists():
            (layer / static / 'assets').mkdir(parents=True)
            for name in ('index.html', 'instructions.html'):
                (layer / static / name).write_text(version)
            (layer / 'image.json').write_text(json.dumps({'version': 'dev', 'revision': version[:1] * 40}))
        env = {**os.environ, 'FAKE': str(fake), 'PATH': f"{fake / 'bin'}:{os.environ['PATH']}",
               'VPN2_DEVELOPMENT': 'true', 'FAKE_OFFLINE': '1' if offline else '0',
               'VPN2_VENV': str(fake / 'venv')}
        env.pop('BASH_ENV', None)
        (fake / 'calls.log').write_text('')
        result = subprocess.run(['bash', str(ROLE / 'files/static.sh'), str(fake / 'home')],
                                env=env, text=True, capture_output=True, timeout=60)
        result.calls = (fake / 'calls.log').read_text().splitlines()
        result.state = json.loads((fake / 'state.json').read_text())
        return result

    def test_static_comes_from_image_and_update_removes_old_image(self):
        with tempfile.TemporaryDirectory(prefix='emergency-static-') as directory:
            fake = self.host(directory)
            home = fake / 'home'
            first = self.static(fake, image='sha256:' + '1' * 64)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.splitlines(), ['image=sha256:' + '1' * 64, 'changed=1'])
            self.assertIn('docker pull ghcr.io/anysda/vpn2/emergency:dev', first.calls)
            # Остановленный контейнер без сети держит образ занятым.
            self.assertEqual(first.state['containers'], {'vpn2-emergency': 'sha256:' + '1' * 64})
            self.assertEqual((home / 'static/instructions.html').read_text(), '1' * 64)
            self.assertEqual(sorted(p.name for p in home.iterdir()), ['release.json', 'static'])
            self.assertEqual(json.loads((home / 'release.json').read_text()),
                             {'version': 'dev', 'revision': '1' * 40, 'image': 'sha256:' + '1' * 64})

            # Повтор с тем же образом узел не трогает.
            again = self.static(fake, image='sha256:' + '1' * 64)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(again.stdout.splitlines(), ['image=sha256:' + '1' * 64])
            self.assertFalse([c for c in again.calls if c.startswith(('docker create', 'docker cp', 'docker rm'))],
                             again.calls)

            second = self.static(fake, image='sha256:' + '2' * 64)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('changed=1', second.stdout.splitlines())
            self.assertEqual(sorted(second.state['images']), ['sha256:' + '2' * 64])
            self.assertEqual(second.state['containers'], {'vpn2-emergency': 'sha256:' + '2' * 64})
            self.assertEqual((home / 'static/instructions.html').read_text(), '2' * 64)
            self.assertFalse((home / 'static.old').exists())
            self.assertEqual(json.loads((home / 'release.json').read_text())['image'], 'sha256:' + '2' * 64)
            self.assertFalse(list(home.glob('.staging.*')))

            # Без реестра установленный образ остаётся и служит дальше.
            third = self.static(fake, image='sha256:' + '2' * 64, offline=True)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(third.stdout.splitlines(), ['image=sha256:' + '2' * 64])
            self.assertEqual(sorted(third.state['images']), ['sha256:' + '2' * 64])

    def test_image_without_static_keeps_installed_page(self):
        with tempfile.TemporaryDirectory(prefix='emergency-static-') as directory:
            fake = self.host(directory)
            home = fake / 'home'
            self.assertEqual(self.static(fake, image='sha256:' + '5' * 64).returncode, 0)
            broken = self.static(fake, image='sha256:' + '6' * 64, static='elsewhere')
            self.assertEqual(broken.returncode, 1, broken.stdout)
            self.assertIn('нет статики страницы', broken.stderr)
            self.assertEqual((home / 'static/index.html').read_text(), '5' * 64)
            self.assertFalse(list(home.glob('.staging.*')))

    # --- задачи роли ----------------------------------------------------------

    def tasks(self, name):
        """Задачи файла роли по порядку; блок наследует своё условие задачам."""
        flat = []
        for task in yaml.safe_load((ROLE / 'tasks' / name).read_text()):
            for inner in task.get('block', [task]):
                flat.append({'when': task.get('when'), **inner})
        return flat

    def names(self, name):
        return [task['name'] for task in self.tasks(name)]

    def test_role_needs_host_venv_before_anything_else(self):
        first = self.tasks('main.yml')[0]
        self.assertEqual(first['ansible.builtin.command']['argv'],
                         ['{{ emergency_vpn2 }}', 'emergency', 'agent', '--help'])
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual(defaults['emergency_venv'], '/opt/vpn2/venv')

    def test_exit_docker_never_touches_the_firewall(self):
        self.assertEqual(json.loads((ROLE / 'files/docker-daemon.json').read_text()),
                         {'bridge': 'none', 'iptables': False, 'ip6tables': False})
        tasks = {task['name']: task for task in self.tasks('main.yml')}
        self.assertEqual(tasks['Настройки Docker']['when'], 'not emergency_entry')
        self.assertEqual(tasks['Настройки Docker']['ansible.builtin.copy']['dest'], '/etc/docker/daemon.json')
        # Настройки лежат до установки: Docker ни разу не стартует с мостом.
        order = self.names('main.yml')
        self.assertLess(order.index('Настройки Docker'), order.index('Пакеты аварийки и Docker'))
        idle = next(task for task in self.tasks('service.yml') if task['name'].startswith('Docker экзита'))
        self.assertEqual(idle['when'], 'not emergency_entry')
        step = idle['vars']['emergency_docker_idle']
        self.assertIn('systemctl disable --now docker.service containerd.service', step)
        self.assertIn('systemctl enable --now docker.socket', step)

    def test_hysteria_and_certificate_belong_to_exit_snapshot_to_entry(self):
        imports = {task['name']: task for task in self.tasks('main.yml') if 'ansible.builtin.import_tasks' in task}
        self.assertEqual(imports['Аварийный вход экзита']['ansible.builtin.import_tasks'], 'exit.yml')
        self.assertEqual(imports['Аварийный вход экзита']['when'], 'not emergency_entry')
        for name in ('main.yml', 'service.yml'):
            text = (ROLE / 'tasks' / name).read_text()
            for needle in ('hy2', 'caddy', 'vpn2-tls'):
                self.assertNotIn(needle, text.lower(), name)
        # VPN2-514: список клиентов экзитам передаёт только entry, своей службой.
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual(defaults['emergency_services'],
                         "{{ ['vpn2-emergency'] + (['vpn2-emergency-snapshot'] if emergency_entry else []) }}")
        for task in self.tasks('main.yml') + self.tasks('service.yml'):
            if 'emergency_services' in json.dumps(task):
                self.assertNotIn('vpn2-emergency-snapshot', json.dumps(task))
        unit = (ROLE / 'templates/vpn2-emergency-snapshot.service.j2').read_text()
        self.assertIn('ExecStart=/opt/vpn2/venv/bin/vpn2 emergency snapshot-sender run', unit)
        self.assertIn('WantedBy=multi-user.target', unit)

    def test_exit_keeps_lets_encrypt_ip_certificate_for_the_emergency(self):
        """VPN2-517: деплой держит сертификат на IP экзита, авария стартует с ним."""
        tasks = {task['name']: task for task in self.tasks('exit.yml')}
        prepare = tasks['Настройки TLS и сниппеты Caddy']['ansible.builtin.command']['argv']
        for flag, value in (('--role', 'exit'), ('--host', '{{ emergency_public_ip }}'),
                            ('--output-dir', '/etc/vpn2/tls/ip'), ('--cert-group', 'vpn2-emergency')):
            self.assertEqual(prepare[prepare.index(flag) + 1], value)
        # Caddy здесь только клиент ACME: HTTP-01 на :80, 443 остаётся странице.
        ports = [item['port'] for item in tasks['Порты аварийного входа и выпуска сертификата']['loop']]
        self.assertEqual(ports, ['{{ emergency_hy2_port }}/udp', '443/tcp', '80/tcp'])
        restart = tasks['Caddy идёт с текущими настройками']['vars']['emergency_caddy_restart']
        self.assertLess(restart.index('caddy validate --config /etc/caddy/Caddyfile'),
                        restart.index('svc_restart_if_changed caddy'))
        # Продление раз в минуту держит выгруженную пару свежей между деплоями.
        self.assertIn('OnUnitInactiveSec=1min', (ROLE / 'templates/vpn2-tls.timer.j2').read_text())
        # Сертификат страницы готовится после выгрузки и стартует на доверенной паре.
        order = self.names('exit.yml')
        self.assertLess(order.index('Первый сертификат на IP'), order.index('Таймер TLS идёт'))
        self.assertLess(order.index('Таймер TLS идёт'), order.index('Сертификат страницы аварийки'))
        page = tasks['Сертификат страницы аварийки']['ansible.builtin.command']['argv']
        self.assertEqual(page[:4], ['runuser', '-u', 'vpn2-emergency', '--'])

    def test_stamp_tells_verify_to_expect_the_image(self):
        # 99-verify требует образ только после этой стадии: пустую отметку
        # оставляла стадия до образа.
        written, last = self.tasks('service.yml')[-2:]
        self.assertEqual(written['ansible.builtin.copy']['dest'], '/var/anysda/.stamps/23-emergency')
        self.assertEqual(written['ansible.builtin.copy']['content'], 'image {{ emergency_image }}\n')
        # Verify узнаёт пройденную стадию по времени отметки: оно новее штампа
        # 10-foreign и после повторного деплоя без правок.
        self.assertEqual(last['ansible.builtin.file'],
                         {'path': '/var/anysda/.stamps/23-emergency', 'state': 'touch', 'mode': '0644'})
        self.assertIs(last['changed_when'], False)
        leftovers = next(task for task in self.tasks('service.yml') if task['name'].startswith('Копии пакета'))
        self.assertEqual(leftovers['loop'], ['vpn2', 'vpn2_emergency'])
        self.assertEqual(leftovers['ansible.builtin.file']['state'], 'absent')


if __name__ == '__main__':
    unittest.main(verbosity=2)
