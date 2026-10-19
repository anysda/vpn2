"""probe_control.py on an installed entry: strongSwan unit and installation restore.

Fake systemd and tools; the installation is a temporary root. VPN2_TEST_ROOT
selects an older checkout for the before/after regression.
"""

import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
TOOL = """#!/usr/bin/python3
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(' '.join([name, *args]) + '\\n')
if name == 'systemctl':
    units = os.environ['FAKE_UNITS'].split()
    unit = args[-1].removesuffix('.service')
    if args[0] == 'show':
        print('loaded' if unit in units else 'not-found')
    elif args[0] == 'is-active':
        sys.exit(0 if args[-1] in os.environ['FAKE_ACTIVE'].split() else 3)
    elif args[0] in ('restart', 'start') and unit.startswith('strongswan') and unit not in units:
        sys.exit('Failed to restart ' + unit + '.service: Unit ' + unit + '.service not found.')
elif name == 'wg':
    print('cHJvYmUtdGVzdC1rZXktMDAwMDAwMDAwMDAwMDAwMDA=')
elif name == 'ip' and args[:2] == ['link', 'show']:
    sys.exit(1)
elif name == 'id':
    sys.exit(1)
elif name == 'openssl':
    for flag in ('-out', '-keyout'):
        if flag in args:
            Path(args[args.index(flag) + 1]).write_text('fake ' + args[0] + '\\n')
"""
INSTALLED = {
    'etc/wireguard/wg0.conf': ('[Interface]\nPrivateKey = installed\n[Peer]\nPublicKey = a\n', 0o600),
    'etc/wireguard/server.priv': ('installed-private\n', 0o600),
    'etc/openvpn/server/server.conf': ('port 1194\n', 0o644),
    'etc/openvpn/server/pki/ca.key': ('installed CA key\n', 0o600),
    'etc/openvpn/server/pki/ca.crt': ('installed CA\n', 0o644),
    'etc/openvpn/server/pki/index.txt': ('V\t300101000000Z\t\t01\tunknown\t/CN=dev_1\n', 0o644),
    'etc/openvpn/server/pki/openssl.cnf': ('[ca]\n', 0o644),
    'etc/openvpn/server/ccd/dev_1': ('ifconfig-push 10.67.67.3 255.255.255.0\n', 0o644),
    'etc/swanctl/swanctl.conf': ('include conf.d/*.conf\n', 0o644),
    'etc/swanctl/conf.d/anysda.conf': ('connections { anysda-ikev2 {} }\n', 0o644),
    'etc/swanctl/conf.d/anysda-clients.conf': ('secrets {}\n', 0o600),
    'etc/swanctl/x509/anysda-server.crt': ('installed server\n', 0o644),
    'etc/swanctl/private/anysda-server.key': ('installed server key\n', 0o600),
    'var/lib/vpn2-control/wg.json': ('{"version": 41}\n', 0o600),
    'var/lib/vpn2-control/ikev2-identities.json': ('{"device1": "x"}\n', 0o600),
}
ACTIVE = 'wg-quick@wg0.service openvpn-server@server.service strongswan-starter.service vpn2-control.socket'


def tree(root):
    rows = []
    for top in ('etc/wireguard', 'etc/openvpn/server', 'etc/swanctl', 'var/lib/vpn2-control'):
        for path in sorted((root / top).rglob('*')):
            info = path.lstat()
            body = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''
            rows.append((str(path.relative_to(root)), oct(info.st_mode), body))
    return rows


class ProbeControlTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        work = Path(self.work.name)
        self.root = work / 'root'
        for name, (body, mode) in INSTALLED.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            path.chmod(mode)
        (self.root / 'etc/swanctl/x509ca').mkdir()
        (self.root / 'etc/wireguard').chmod(0o700)
        (self.root / 'var/lib/vpn2-control').chmod(0o700)
        (self.root / 'tmp').mkdir()
        self.installed = tree(self.root)
        tools = work / 'bin'
        tools.mkdir()
        for name in ('systemctl', 'ip', 'wg', 'openssl', 'id', 'useradd', 'userdel', 'runuser'):
            (tools / name).write_text(TOOL)
            (tools / name).chmod(0o755)
        self.log = work / 'calls.log'
        self.log.touch()
        environment = {'PATH': f'{tools}:/usr/bin:/bin', 'FAKE_LOG': str(self.log),
                       'FAKE_UNITS': 'strongswan-starter openvpn-server@control-test',
                       'FAKE_ACTIVE': ACTIVE, 'VPN2_CONTROL_TEST': '1'}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location('probe_control_under_test',
                                                      ROOT / 'infra/tests/probe_control.py')
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)
        self.probe.ROOT = self.root

    def run_probe(self, exercise):
        with mock.patch.object(self.probe, 'exercise', exercise), \
                mock.patch.object(self.probe.os, 'getuid', return_value=0), \
                mock.patch.object(self.probe.sys, 'argv', ['probe_control.py']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.probe.main()

    def calls(self, tool='systemctl'):
        return [line.split(' ', 1)[1] for line in self.log.read_text().splitlines()
                if line.startswith(tool + ' ')]

    def control_api(self):
        # What the live API does to the fixture's files during exercise().
        root = self.root
        (root / 'etc/openvpn/server/ccd/dev_7').write_text('disable\n')
        (root / 'etc/openvpn/server/pki/index.txt').write_text('R\t\t\t01\tunknown\t/CN=dev_7\n')
        (root / 'etc/wireguard/wg0.conf').write_text('[Interface]\nPrivateKey = fixture\n')
        (root / 'etc/swanctl/conf.d/anysda-clients.conf').write_text('secrets { eap-7 {} }\n')
        (root / 'var/lib/vpn2-control/wg.json').write_text('{"version": 3}\n')
        (root / 'tmp/vpn2-svc').mkdir(exist_ok=True)

    def test_starter_unit_restarted_and_installation_byte_identical(self):
        self.run_probe(self.control_api)
        calls = self.calls()
        self.assertIn('restart strongswan-starter', calls)
        self.assertNotIn('restart strongswan', calls)
        self.assertEqual(tree(self.root), self.installed)
        self.assertEqual(sorted(self.root.glob('**/*.vpn2-probe-saved')), [])
        self.assertFalse((self.root / 'tmp/vpn2-svc').exists())
        self.assertEqual(calls[-4:], ['start wg-quick@wg0.service', 'start openvpn-server@server.service',
                                      'restart strongswan-starter.service', 'start vpn2-control.socket'])

    def test_failed_run_still_restores_installation(self):
        def broken():
            self.control_api()
            raise AssertionError('control API failed mid-run')

        with self.assertRaisesRegex(AssertionError, 'mid-run'):
            self.run_probe(broken)
        self.assertEqual(tree(self.root), self.installed)
        self.assertIn('restart strongswan-starter', self.calls())

    def test_missing_strongswan_unit_changes_nothing(self):
        os.environ['FAKE_UNITS'] = 'strongswan openvpn-server@control-test'
        with self.assertRaises(SystemExit) as raised:
            self.run_probe(self.control_api)
        self.assertIn('strongswan-starter.service is not installed', str(raised.exception))
        self.assertEqual(tree(self.root), self.installed)
        self.assertEqual([call for call in self.calls() if not call.startswith('show ')], [])

    def test_interrupted_run_is_recovered_first(self):
        wireguard = self.root / 'etc/wireguard'
        wireguard.rename(self.root / 'etc/wireguard.vpn2-probe-saved')
        wireguard.mkdir(mode=0o700)
        (wireguard / 'wg0.conf').write_text('[Interface]\nPrivateKey = fixture-of-killed-run\n')
        self.run_probe(self.control_api)
        self.assertEqual(tree(self.root), self.installed)

    def test_killed_run_socket_path_is_given_back(self):
        drop_in = self.root / 'run/systemd/system/vpn2-control.socket.d/zz-probe.conf'
        drop_in.parent.mkdir(parents=True)
        drop_in.write_text('[Socket]\nListenStream=\nListenStream=/run/vpn2-control-probe/control.sock\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.probe.preserve()
        self.assertIn('interrupted probe run found', output.getvalue())
        self.probe.put_back()
        self.assertFalse(drop_in.exists())
        self.assertEqual(self.listen_path(), self.root / 'run/vpn2-control/control.sock')

    def test_group_only_accepts_refusal_sent_before_the_request(self):
        # A service that refuses by peer credentials and closes before reading
        # the request: the probe's write then fails with EPIPE.
        address = str(self.root / 'control.sock')
        closed = threading.Event()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(address)
            listener.listen(1)

            def refuse():
                connection, _address = listener.accept()
                with connection:
                    connection.sendall(b'{"api": 1, "ok": false, "error": "forbidden"}\n')
                closed.set()

            worker = threading.Thread(target=refuse)
            worker.start()
            real = socket.socket

            class Late(real):
                def connect(self, _path):
                    super().connect(address)
                    closed.wait(5)

            with mock.patch.object(self.probe.socket, 'socket', Late), \
                    mock.patch.object(self.probe.sys, 'argv', ['probe_control.py', '--group-only']):
                self.probe.main()
            worker.join(5)

    def listen_path(self):
        """Where systemd puts vpn2-control.socket: the unit plus its drop-ins."""
        paths = []
        units = [ROOT / 'infra/control/vpn2-control.socket']
        for base in ('etc/systemd/system', 'run/systemd/system'):
            units += sorted((self.root / base / 'vpn2-control.socket.d').glob('*.conf'))
        for unit in units:
            for line in unit.read_text().splitlines():
                if line.startswith('ListenStream='):
                    value = line.split('=', 1)[1]
                    paths = [value] if value else []
        self.assertEqual(len(paths), 1)
        return self.root / paths[0].lstrip('/')

    @contextlib.contextmanager
    def service(self):
        # The service's own version check over the state directory it sees now.
        sys.path.insert(0, str(ROOT / 'infra/control'))
        self.addCleanup(sys.path.remove, str(ROOT / 'infra/control'))
        spec = importlib.util.spec_from_file_location('backend_under_test', ROOT / 'infra/control/backend.py')
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        state = backend.Backend(self.root, self.root / 'var/lib/vpn2-control')
        path = self.listen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(4)
        listener.settimeout(0.05)
        done = threading.Event()

        def serve():
            while not done.is_set():
                try:
                    connection, _address = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    request = json.loads(connection.makefile('rb').readline())
                    try:
                        state.version(request['method'].split('.')[0], request['params'])
                        reply = {'api': 1, 'ok': True, 'result': {}}
                    except Exception as error:
                        reply = {'api': 1, 'ok': False, 'error': str(error)}
                    connection.sendall(json.dumps(reply).encode() + b'\n')

        worker = threading.Thread(target=serve)
        worker.start()
        try:
            yield
        finally:
            done.set()
            worker.join(5)
            listener.close()
            path.unlink()

    def panel(self, version):
        """The panel container's view: /run/vpn2-control bind-mounted."""
        client = {'device_id': 3, 'username': 'device3', 'password': 'panel-password-1', 'ip': '10.68.68.4'}
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5)
            try:
                connection.connect(str(self.root / 'run/vpn2-control/control.sock'))
            except OSError as error:
                return error
            connection.sendall(json.dumps({'api': 1, 'method': 'ikev2.apply',
                                           'params': {'version': version, 'clients': [client]}}).encode() + b'\n')
            return json.loads(connection.makefile('rb').readline())

    def test_other_caller_during_probe_neither_changes_it_nor_gets_ok_from_fixture(self):
        root = self.root
        real = socket.socket

        class Rooted(real):
            # Older probes connect to the absolute path; map it into the test root.
            def connect(self, path):
                if not str(path).startswith(str(root)):
                    path = str(root) + str(path)
                super().connect(path)

        replies = {}

        def exercise():
            with self.service():
                replies['panel'] = self.panel(5)
                client = {'device_id': 7, 'username': 'device7', 'password': 'test-password-123', 'ip': '10.68.68.2'}
                replies['probe'] = self.probe.request('ikev2.apply', {'version': 1, 'clients': [client]})

        with mock.patch.object(self.probe.socket, 'socket', Rooted):
            self.run_probe(exercise)
        self.assertEqual(replies['probe'], {'api': 1, 'ok': True, 'result': {}})
        self.assertIsInstance(replies['panel'], OSError)
        self.assertEqual(sorted((root / 'run/systemd').rglob('*.conf')), [])
        # After the run the installation listens where the panel retries.
        with self.service():
            self.assertEqual(self.panel(5), {'api': 1, 'ok': True, 'result': {}})
        self.assertGreaterEqual(json.loads((root / 'var/lib/vpn2-control/ikev2.json').read_text())['version'], 5)
        self.assertIn('daemon-reload', self.calls())


if __name__ == '__main__':
    unittest.main()
