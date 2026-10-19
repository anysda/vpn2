"""The emergency front ships as a registry image and is installed in standby.

Stage 23 runs for real against a disposable root with fake host commands:
docker, apt and systemd only record what the stage asks of them.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

import test_release


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

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

# Records the call; installing docker-ce places the docker command in PATH.
RECORDER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

fake = Path(os.environ['FAKE'])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state = json.loads((fake / 'state.json').read_text())
line = name + ' ' + ' '.join(args)
if name == 'apt-get' and 'install' in args:
    line += ' [daemon.json=%s]' % (fake / 'root/etc/docker/daemon.json').exists()
    state['packages'] += [a for a in args if not a.startswith('-') and a != 'install' and not a.endswith('-')]
    if 'docker-ce' in args:
        shutil.copy(fake / 'docker', fake / 'bin/docker')
    (fake / 'state.json').write_text(json.dumps(state))
with open(fake / 'calls.log', 'a') as log:
    log.write(line + '\n')
if name == 'dpkg-query':
    packages = args[2:]
    print('\n'.join('installed' if p in state['packages'] else 'not-installed' for p in packages))
elif name == 'dpkg':
    print('amd64')
elif name == 'getent':
    sys.exit(0 if 'vpn2-emergency' in state['users'] else 2)
elif name == 'useradd':
    state['users'].append(args[-1])
    (fake / 'state.json').write_text(json.dumps(state))
elif name == 'systemctl' and args[:2] == ['is-active', '--quiet']:
    sys.exit(0 if args[2] == 'vpn2-emergency.service' else 3)
elif name == 'fuser':
    sys.exit(1)
elif name == 'curl':
    print('public-key')
elif name == 'gpg':
    Path(args[-1]).write_text('key')
'''

# install(1) as root sets owners; the fixture drops them and keeps the rest.
INSTALL = '''#!/usr/bin/env python3
import os
import sys
args, skip = [], False
for arg in sys.argv[1:]:
    if skip:
        skip = False
    elif arg in ('-o', '-g'):
        skip = True
    else:
        args.append(arg)
os.execv('/usr/bin/install', ['install', *args])
'''

# The image's own installer is exercised by its own tests; here it only logs.
HY2_INSTALLER = '''#!/usr/bin/env bash
echo "install-hy2 $*" >> "$FAKE/calls.log"
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

    def test_image_carries_built_static_agent_and_installer(self):
        text = (ROOT / 'infra/emergency/Dockerfile').read_text()
        self.assertIn('RUN pnpm emergency:build', text)
        carrier = text.rsplit('\nFROM ', 1)[1]
        self.assertTrue(carrier.startswith('scratch'), 'carrier needs no runtime of its own')
        for needle in ('vpn2_emergency/static /opt/vpn2-emergency/vpn2_emergency/static',
                       'infra/emergency/vpn2_emergency/*.py /opt/vpn2-emergency/vpn2_emergency/',
                       'infra/emergency/install-hy2.sh', 'vpn2-emergency-hy2.service',
                       'vpn2-emergency-hy2-probe.service', 'image.json', 'LABEL vpn2.component=emergency'):
            self.assertIn(needle, carrier)
        ignore = (ROOT / 'infra/emergency/Dockerfile.dockerignore').read_text().splitlines()
        for needle in ('*', '!app', '!infra/emergency', 'app/node_modules',
                       'infra/emergency/vpn2_emergency/static', '**/.env'):
            self.assertIn(needle, ignore)

    def test_release_manifest_pins_emergency_image(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = test_release.ReleaseTests().build_manifest(Path(directory), migrations_after_tag=0)
        self.assertEqual(sorted(manifest['images']), ['emergency', 'panel', 'tgbot'])
        result = subprocess.run([sys.executable, str(ROOT / 'infra/lib/release.py'), '--manifest',
                                 '/nonexistent/release-manifest.json', 'image', 'emergency', '--offline'],
                                capture_output=True, text=True)
        self.assertNotIn('invalid choice', result.stderr)
        self.assertIn('release manifest not found', result.stderr)

    def test_activation_code_downloads_and_installs_nothing(self):
        files = list((ROOT / 'infra/emergency/vpn2_emergency').glob('*.py'))
        files.append(ROOT / 'infra/emergency/install-hy2.sh')
        for path in files:
            text = path.read_text()
            for needle in ('apt-get', 'apt install', 'dpkg -i', 'pip install', 'docker ', 'ghcr.io'):
                self.assertNotIn(needle, text, f'{path.name}: {needle}')

    # --- stage 23 against a fake host ---------------------------------------

    def stage(self, fake, host, *, image, offline=False):
        """Run stage 23 for host with `image` published under the dev tag."""
        state = json.loads((fake / 'state.json').read_text())
        state['registry']['ghcr.io/anysda/vpn2/emergency:dev'] = image
        (fake / 'state.json').write_text(json.dumps(state))
        version = image.split(':')[1]
        layer = fake / 'image' / version
        if not layer.exists():
            shutil.copytree(ROOT / 'infra/emergency/vpn2_emergency', layer / 'vpn2_emergency',
                            ignore=shutil.ignore_patterns('static', '__pycache__'))
            (layer / 'vpn2_emergency/static/assets').mkdir(parents=True)
            for name in ('index.html', 'instructions.html'):
                (layer / 'vpn2_emergency/static' / name).write_text(version)
            (layer / 'install-hy2.sh').write_text(HY2_INSTALLER)
            (layer / 'image.json').write_text(json.dumps({'version': 'dev', 'revision': version[:1] * 40}))
        root = fake / 'root'
        config = fake / 'emergency.json'
        config.write_text(json.dumps({'node': host, 'nodes': {
            'ru': {'host': '198.51.100.10', 'port': 51821},
            'exit': {'host': '198.51.100.20', 'port': 51821}}}))
        for folder in ('etc/apt/sources.list.d', 'etc/systemd/system', 'etc/vpn2/tls'):
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_CODENAME=resolute\n')
        (root / 'opt/vpn2-emergency').mkdir(parents=True, exist_ok=True)
        stage_dir = fake / 'stage'
        stage_dir.mkdir(exist_ok=True)
        ref = os.environ.get('STAGE_REF')
        source = subprocess.check_output(['git', 'show', f'{ref}:infra/scripts/23-emergency.sh'], cwd=ROOT,
                                         text=True) if ref else (ROOT / 'infra/scripts/23-emergency.sh').read_text()
        for path in ('/opt/vpn2-emergency', '/etc/', '/var/lib/vpn2-emergency', '/var/anysda'):
            source = source.replace(path, str(root) + path)
        (stage_dir / '23-emergency.sh').write_text(source)
        svc = (ROOT / 'infra/lib/anysda-svc.sh').read_text().replace('/etc/', str(root) + '/etc/')
        (stage_dir / 'anysda-svc.sh').write_text(svc)
        for name in ('release.sh', 'release.py'):
            shutil.copy(ROOT / 'infra/lib' / name, stage_dir / name)
        env = {**os.environ, 'FAKE': str(fake), 'PATH': f"{fake / 'bin'}:{fake / 'sbin'}",
               'HOST_TAG': host, 'VPN2_DEVELOPMENT': 'true', 'EMERGENCY_CONFIG_SOURCE': str(config),
               'FAKE_OFFLINE': '1' if offline else '0'}
        env.pop('BASH_ENV', None)
        (fake / 'calls.log').write_text('')
        result = subprocess.run(['bash', str(stage_dir / '23-emergency.sh')], env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        result.calls = (fake / 'calls.log').read_text().splitlines()
        result.state = json.loads((fake / 'state.json').read_text())
        return result

    def host(self, directory):
        fake = Path(directory)
        (fake / 'bin').mkdir()
        # The host's own tools without docker: a fresh exit has none.
        (fake / 'sbin').mkdir()
        for folder in ('/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'):
            for tool in Path(folder).iterdir() if Path(folder).is_dir() else ():
                link = fake / 'sbin' / tool.name
                if tool.name not in ('docker', 'dockerd') and not os.path.lexists(link):
                    link.symlink_to(tool)
        (fake / 'docker').write_text(DOCKER)
        (fake / 'docker').chmod(0o755)
        for name in ('apt-get', 'dpkg-query', 'dpkg', 'getent', 'useradd', 'systemctl', 'ufw',
                     'runuser', 'fuser', 'curl', 'gpg'):
            (fake / 'bin' / name).write_text(RECORDER)
            (fake / 'bin' / name).chmod(0o755)
        (fake / 'bin/install').write_text(INSTALL)
        (fake / 'bin/install').chmod(0o755)
        (fake / 'state.json').write_text(json.dumps(
            {'registry': {}, 'images': {}, 'containers': {}, 'users': [],
             'packages': ['ca-certificates', 'curl', 'gnupg', 'openssl', 'nftables', 'qrencode']}))
        return fake

    def test_exit_gets_image_in_standby_and_update_removes_old_image(self):
        with tempfile.TemporaryDirectory(prefix='emergency-stage-') as directory:
            fake = self.host(directory)
            root = fake / 'root'
            first = self.stage(fake, 'exit', image='sha256:' + '1' * 64)
            print(first.stdout, end='')
            self.assertEqual(first.returncode, 0, first.stdout)
            install = [c for c in first.calls if c.startswith('apt-get') and ' install ' in c]
            self.assertEqual(len(install), 1, first.calls)
            for package in ('docker-ce', 'polkitd', 'qrencode', 'nftables', 'openssl'):
                self.assertIn(' ' + package + ' ', install[0] + ' ')
            # Docker on the exit never gets a chance to rewrite the firewall.
            self.assertIn('[daemon.json=True]', install[0])
            self.assertEqual(json.loads((root / 'etc/docker/daemon.json').read_text()),
                             {'bridge': 'none', 'iptables': False, 'ip6tables': False})
            self.assertIn('docker pull ghcr.io/anysda/vpn2/emergency:dev', first.calls)
            self.assertEqual(first.state['containers'], {'vpn2-emergency': 'sha256:' + '1' * 64})
            package = root / 'opt/vpn2-emergency/vpn2_emergency'
            self.assertTrue((package / 'agent.py').is_file())
            self.assertEqual((package / 'static/instructions.html').read_text(), '1' * 64)
            release = json.loads((root / 'opt/vpn2-emergency/release.json').read_text())
            self.assertEqual(release, {'version': 'dev', 'revision': '1' * 40, 'image': 'sha256:' + '1' * 64})
            self.assertIn('install-hy2 198.51.100.20 18443', first.calls)
            self.assertTrue(any(c.startswith('runuser -u vpn2-emergency') for c in first.calls), first.calls)
            unit = (root / 'etc/systemd/system/vpn2-emergency.service').read_text()
            self.assertIn('User=vpn2-emergency', unit)
            self.assertIn('systemctl disable --now docker.service containerd.service', first.calls)
            self.assertIn('systemctl enable --now docker.socket', first.calls)
            self.assertIn('23-emergency done: image dev 111111111111', first.stdout)

            second = self.stage(fake, 'exit', image='sha256:' + '2' * 64)
            print(second.stdout, end='')
            self.assertEqual(second.returncode, 0, second.stdout)
            self.assertFalse([c for c in second.calls if c.startswith('apt-get')], second.calls)
            self.assertEqual(sorted(second.state['images']), ['sha256:' + '2' * 64])
            self.assertEqual(second.state['containers'], {'vpn2-emergency': 'sha256:' + '2' * 64})
            self.assertEqual((package / 'static/instructions.html').read_text(), '2' * 64)
            self.assertEqual(json.loads((root / 'opt/vpn2-emergency/release.json').read_text())['image'],
                             'sha256:' + '2' * 64)
            self.assertFalse(list((root / 'opt/vpn2-emergency').glob('.staging.*')))

            # Without the registry the installed image is kept and reused.
            third = self.stage(fake, 'exit', image='sha256:' + '2' * 64, offline=True)
            self.assertEqual(third.returncode, 0, third.stdout)
            self.assertEqual(sorted(third.state['images']), ['sha256:' + '2' * 64])

    def test_entry_keeps_its_docker_and_has_no_hysteria(self):
        with tempfile.TemporaryDirectory(prefix='emergency-stage-') as directory:
            fake = self.host(directory)
            shutil.copy(fake / 'docker', fake / 'bin/docker')
            state = json.loads((fake / 'state.json').read_text())
            state['packages'] = ['qrencode', 'curl', 'openssl', 'nftables', 'polkitd']
            (fake / 'state.json').write_text(json.dumps(state))
            result = self.stage(fake, 'ru', image='sha256:' + '3' * 64)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse((fake / 'root/etc/docker/daemon.json').exists())
            self.assertFalse([c for c in result.calls if c.startswith(('apt-get', 'install-hy2', 'runuser'))],
                             result.calls)
            self.assertFalse([c for c in result.calls if 'docker.service' in c], result.calls)
            self.assertEqual(result.state['containers'], {'vpn2-emergency': 'sha256:' + '3' * 64})


if __name__ == '__main__':
    unittest.main(verbosity=2)
