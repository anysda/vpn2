"""Local control API contract, credential boundary, and failure regressions."""
import base64
import hashlib
from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
CONTROL = ROOT / 'infra/control'


def module(name):
    spec = importlib.util.spec_from_file_location(name, CONTROL / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = module('contract')
        cls.backend_module = module('backend')
        cls.server = module('server')
        cls.pools = json.loads((ROOT / 'app/shared/vpn-pools.json').read_text())

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.backend = self.backend_module.Backend(self.root, self.state)
        self.backend.pools = self.pools
        self.public = base64.b64encode(bytes(range(32))).decode()
        self.psk = base64.b64encode(bytes(range(1, 33))).decode()
        self.peer = {'key': self.public, 'preshared_key': self.psk, 'ip': '10.66.66.2'}

    def request(self, method, params, allowed=True, raw=None, host_uid=None):
        client, server = socket.socketpair()
        with client, server:
            client.settimeout(5)
            uid = os.getuid() if host_uid is None else host_uid
            peer = server
            if host_uid is not None:
                peer = Mock(wraps=server)
                peer.getsockopt.return_value = struct.pack('iII', os.getpid(), uid, uid)
            worker = threading.Thread(target=self.server.handle,
                                      args=(peer, self.backend,
                                            {uid} if allowed else {uid + 1},
                                            self.pools))
            worker.start()
            data = raw or json.dumps({'api': 1, 'method': method, 'params': params}).encode() + b'\n'
            try:
                client.sendall(data)
                reply = json.loads(client.makefile('rb').readline())
            finally:
                worker.join(5)
            self.assertFalse(worker.is_alive())
            return reply

    def invalid(self, method, params, code='validation'):
        self.assertEqual(self.request(method, params), {'api': 1, 'ok': False, 'error': code})

    def file(self, name, body):
        path = self.root / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_methods_ready_and_no_general_execution(self):
        self.assertEqual(self.request('check.run', {'check': 'readiness'})['result'],
                         {'ready': True, 'api': 1})
        for method in ('shell', 'file.write', 'service.restart', 'secret.read'):
            self.invalid(method, {})

    def test_path_extra_fields_and_nested_injection(self):
        self.invalid('config.apply', {'resource': '/etc/shadow', 'schema': 1,
                                     'version': 1, 'config': {}})
        self.invalid('config.apply', {'resource': 'wg-peers', 'schema': 1, 'version': 1,
                                     'config': {'peers': [], 'path': '/etc/shadow'}})
        self.invalid('wg.apply', {'version': 1, 'peers': [], 'path': '/etc/shadow'})
        self.invalid('wg.apply', {'version': 1, 'peers': [dict(self.peer, command='id')]})
        self.invalid('openvpn.apply', {'version': 1, 'device_id': '../../etc/shadow',
                                       'action': 'deny', 'ip': '10.67.67.2'})
        self.invalid('check.run', {'check': 'id; touch /tmp/injected'})

    def test_peer_credentials_even_when_socket_is_accessible(self):
        self.assertEqual(self.request('check.run', {'check': 'readiness'}, allowed=False),
                         {'api': 1, 'ok': False, 'error': 'forbidden'})

    def test_forbidden_caller_gets_reply_not_broken_pipe(self):
        # The service closes each connection after handle(), as serve() does.
        # A refused caller that writes after that point used to get EPIPE.
        for delay in (0, 0.2):
            client, server = socket.socketpair()
            with client:
                client.settimeout(5)
                def serve_one():
                    with server:
                        self.server.handle(server, self.backend, {os.getuid() + 1}, self.pools)
                log = io.StringIO()
                with redirect_stderr(log):
                    worker = threading.Thread(target=serve_one)
                    worker.start()
                    time.sleep(delay)
                    client.sendall(b'{"api":1,"method":"check.run","params":{"check":"readiness"}}\n')
                    reply = client.makefile('rb').readline()
                    worker.join(5)
                self.assertEqual(json.loads(reply), {'api': 1, 'ok': False, 'error': 'forbidden'})
                self.assertEqual(client.recv(1), b'')
                self.assertIn(f'forbidden caller uid={os.getuid()}', log.getvalue())

    def test_high_mapped_host_uid_is_not_interpreted_as_signed(self):
        # Linux uid_t is unsigned, including host IDs mapped from containers.
        self.assertTrue(self.request('check.run', {'check': 'readiness'}, host_uid=3000000000)['ok'])
        self.assertEqual(self.request('check.run', {'check': 'readiness'}, allowed=False,
                                      host_uid=3000000000)['error'], 'forbidden')

    def test_size_and_duplicate_json_fields(self):
        self.invalid('wg.apply', {'version': 1, 'peers': [], 'padding': 'x' * self.contract.LIMIT},
                     'limit')
        result = self.request('', {}, raw=b'{"api":1,"api":1,"method":"check.run",'
                                         b'"params":{"check":"readiness"}}\n')
        self.assertEqual(result['error'], 'validation')

    def test_types_keys_addresses_and_versions(self):
        for version in (True, 0, -1, 1.2, '1', 2**53):
            self.invalid('wg.apply', {'version': version, 'peers': []})
        for key in ('bad', base64.b64encode(bytes(32)).decode(), self.public + '\nid'):
            self.invalid('wg.apply', {'version': 1, 'peers': [dict(self.peer, key=key)]})
        for ip in ('127.0.0.1', '0.0.0.0/0', '10.66.66.1', '10.66.66.255',
                   '10.66.66.2\nPostUp=id', '::1'):
            self.invalid('wg.apply', {'version': 1, 'peers': [dict(self.peer, ip=ip)]})
        self.invalid('wg.apply', {'version': 1, 'peers': [self.peer, self.peer]})
        self.invalid('wg.apply', {'version': 1, 'peers': [dict(self.peer, preshared_key='bad')]})

    def test_versions_survive_failure_restart_and_config_alias(self):
        params = {'version': 4, 'peers': [self.peer]}
        with patch.object(self.backend, 'wireguard', side_effect=OSError('sensitive value')):
            self.invalid('wg.apply', params, 'unavailable')
        self.backend = self.backend_module.Backend(self.root, self.state)
        self.invalid('wg.apply', dict(params, version=3), 'stale')
        self.invalid('config.apply', {'resource': 'wg-peers', 'schema': 1, 'version': 4,
                                     'config': {'peers': []}}, 'conflict')
        with patch.object(self.backend, 'wireguard', return_value={'peers': {}}) as apply:
            self.assertTrue(self.request('wg.apply', params)['ok'])
            self.assertTrue(self.request('wg.apply', params)['ok'])
            self.assertEqual(apply.call_count, 2)

    def test_wg_verifies_kernel_and_preserves_interface(self):
        path = self.file('/etc/wireguard/wg0.conf', '[Interface]\nAddress = 10.66.66.1/24\n'
                         'PrivateKey = server-secret\nListenPort = 51820\n')
        calls = []

        def command(*args, data=None):
            calls.append(args)
            if args[1] == 'syncconf':
                candidate = data
                self.assertNotIn('Address', candidate)
                self.assertIn(self.public, candidate)
                self.assertIn('PresharedKey = ' + self.psk, candidate)
                self.assertIn('fd66:66::2/128', candidate)
                return ''
            if args[-1] == 'preshared-keys':
                return self.public + '\t' + self.psk + '\n'
            return self.public + '\t10.66.66.2/32 fd66:66::2/128\n'

        with patch.object(self.backend, 'command', side_effect=command):
            reply = self.request('wg.apply', {'version': 1, 'peers': [self.peer]})
        self.assertTrue(reply['ok'], reply)
        self.assertNotIn('server-secret', json.dumps(reply))
        self.assertNotIn(self.psk, json.dumps(reply))
        self.assertIn('Address = 10.66.66.1/24', path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(calls[0][:3], ('/usr/bin/wg', 'syncconf', 'wg0'))
        with patch.object(self.backend, 'command', return_value=''):
            self.invalid('wg.apply', {'version': 2, 'peers': [self.peer]}, 'unavailable')

    def test_wg_candidate_goes_through_stdin_for_apparmor(self):
        # Ubuntu's wg profile lets wg open only /etc/wireguard, closed to the
        # service user; a candidate file anywhere is unreadable to wg.
        self.file('/etc/wireguard/wg0.conf', '[Interface]\nPrivateKey = server-secret\n')
        syncs = []

        def command(*args, data=None):
            if args[1] == 'syncconf':
                syncs.append((args, data))
                return ''
            if args[-1] == 'preshared-keys':
                return self.public + '\t' + self.psk + '\n'
            return self.public + '\t10.66.66.2/32 fd66:66::2/128\n'

        with patch.object(self.backend, 'command', side_effect=command):
            self.assertTrue(self.request('wg.apply', {'version': 1, 'peers': [self.peer]})['ok'])
        self.assertEqual(syncs[0][0], ('/usr/bin/wg', 'syncconf', 'wg0', '/dev/stdin'))
        self.assertIn('PublicKey = ' + self.public, syncs[0][1])
        self.assertEqual(self.backend_module.run(['wc', '-c'], time.monotonic() + 5, 'x' * 300000),
                         '300000\n')

    def test_wg_preserves_extended_pool_ula_mapping(self):
        self.assertEqual(self.backend_module.peer_addresses(dict(self.peer, ip='10.66.64.12')),
                         ['10.66.64.12/32', 'fd66:66::64:12/128'])

    def test_wg_keys_only_restore_replaces_the_old_hosts_rendered_key(self):
        path = self.file('/etc/wireguard/wg0.conf',
                         '[Interface]\nPrivateKey = old-host-key\nListenPort = 51820\n')
        self.file('/etc/wireguard/server.priv', self.public + '\n')
        with patch.object(self.backend, 'command', return_value=''):
            reply = self.request('wg.apply', {'version': 1, 'peers': []})
        self.assertTrue(reply['ok'], reply)
        self.assertIn('PrivateKey = ' + self.public + '\n', path.read_text())
        self.assertNotIn('old-host-key', path.read_text())
        self.assertNotIn(self.public, json.dumps(reply))

    def test_openvpn_revocation_uses_managed_certificate_ids(self):
        self.file('/etc/openvpn/server/pki/index.txt',
                  'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        self.file('/etc/openvpn/server/ccd/dev_7', '')
        calls = []

        def command(*args):
            calls.append(args)
            if '-revoke' in args:
                self.file('/etc/openvpn/server/pki/index.txt',
                          'R\t300101000000Z\t260101000000Z\t0A\tunknown\t/CN=dev_7\n')
            if '-gencrl' in args:
                Path(args[-1]).write_text('CRL')
            return ''

        with patch.object(self.backend, 'command', side_effect=command), \
                patch.object(self.backend, 'management', return_value=['END']):
            reply = self.request('openvpn.apply', {'version': 1, 'device_id': 7,
                                                   'action': 'revoke', 'ip': '10.67.67.2'})
            self.assertTrue(reply['ok'], reply)
            self.assertTrue(reply['result']['observed']['revoked'])
            self.invalid('openvpn.apply', {'version': 2, 'device_id': 7,
                                           'action': 'allow', 'ip': '10.67.67.2'}, 'conflict')
        self.assertTrue(calls[0][-1].endswith('/pki/newcerts/0A.pem'))
        self.assertIn('disable', (self.root / 'etc/openvpn/server/ccd/dev_7').read_text())

    def test_openvpn_live_session_must_disappear(self):
        self.file('/etc/openvpn/server/pki/index.txt',
                  'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        self.file('/etc/openvpn/server/ccd/dev_7', '')
        rows = ['CLIENT_LIST,dev_7,192.0.2.2:1,10.67.67.2', 'END']
        params = {'version': 1, 'device_id': 7, 'action': 'deny', 'ip': '10.67.67.2'}
        with patch.object(self.backend, 'management', return_value=rows):
            self.invalid('openvpn.apply', params, 'unavailable')
        with patch.object(self.backend, 'management', side_effect=[rows, ['SUCCESS: killed'], ['END']]) as call:
            self.assertTrue(self.request('openvpn.apply', params)['ok'])
            self.assertIn(unittest.mock.call('kill dev_7'), call.call_args_list)

    def openvpn_ca(self, status='V'):
        self.file('/etc/openvpn/server/server.conf',
                  'crl-verify /etc/openvpn/server/pki/crl.pem\nuser nobody\ngroup nogroup\n')
        self.file('/etc/openvpn/server/pki/index.txt',
                  status + '\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        self.file('/etc/openvpn/server/pki/crl.pem', 'CRL')
        self.file('/etc/openvpn/server/pki/newcerts/0A.pem', 'certificate')
        self.file('/etc/openvpn/server/ccd/dev_7', 'ifconfig-push 10.67.67.2 255.255.255.0\n')
        for directory in ('etc', 'etc/openvpn', 'etc/openvpn/server', 'etc/openvpn/server/pki',
                          'etc/openvpn/server/ccd'):
            (self.root / directory).chmod(0o755)
        failures = []

        def command(*args, **_options):
            if failures and failures[0] in args:
                raise self.contract.Fault('unavailable')
            if '-revoke' in args:
                self.file('/etc/openvpn/server/pki/index.txt',
                          'R\t300101000000Z\t260101000000Z\t0A\tunknown\t/CN=dev_7\n')
            if '-gencrl' in args:
                Path(args[-1]).write_text('CRL')
            if '-text' in args:
                return 'Revoked Certificates:\n    Serial Number: 0A\n'
            return ''
        return command, failures

    def test_openvpn_crl_the_server_cannot_read_is_not_a_revocation(self):
        # OpenVPN re-reads crl.pem as user nobody; root's openssl verify
        # passing does not mean the server stopped accepting the certificate.
        command, _failures = self.openvpn_ca()
        pki = self.root / 'etc/openvpn/server/pki'
        pki.chmod(0o700)
        params = {'version': 1, 'device_id': 7, 'action': 'revoke', 'ip': '10.67.67.2'}
        with patch.object(self.backend, 'command', side_effect=command), \
                patch.object(self.backend, 'management', return_value=['END']):
            self.invalid('openvpn.apply', params, 'unavailable')
            state = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
            self.assertFalse(state['result']['revoked'], state)
            pki.chmod(0o755)
            self.assertTrue(self.request('openvpn.apply', params)['ok'])
            state = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
            self.assertTrue(state['result']['revoked'], state)

    def test_openvpn_ccd_the_server_cannot_read_is_not_a_deny(self):
        command, _failures = self.openvpn_ca()
        params = {'version': 1, 'device_id': 7, 'action': 'deny', 'ip': '10.67.67.2'}
        ccd = self.root / 'etc/openvpn/server/ccd'
        ccd.chmod(0o700)
        with patch.object(self.backend, 'command', side_effect=command), \
                patch.object(self.backend, 'management', return_value=['END']):
            self.invalid('openvpn.apply', params, 'unavailable')
            state = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
            self.assertTrue(state['result']['allowed'], state)
            ccd.chmod(0o755)
            self.assertTrue(self.request('openvpn.apply', params)['ok'])
            state = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
            self.assertFalse(state['result']['allowed'], state)

    def test_openvpn_same_second_same_size_crl_is_still_reloaded(self):
        # OpenVPN compares whole-second mtime and size before reloading.
        command, _failures = self.openvpn_ca()
        crl = self.root / 'etc/openvpn/server/pki/crl.pem'
        before = int(crl.stat().st_mtime)
        with patch.object(self.backend, 'command', side_effect=command), \
                patch.object(self.backend, 'management', return_value=['END']):
            self.assertTrue(self.request('openvpn.apply', {'version': 1, 'device_id': 7,
                                                           'action': 'revoke', 'ip': '10.67.67.2'})['ok'])
        self.assertEqual(crl.read_text(), 'CRL')
        self.assertGreater(int(crl.stat().st_mtime), before)

    def test_openvpn_server_must_verify_the_managed_crl(self):
        command, _failures = self.openvpn_ca()
        self.file('/etc/openvpn/server/server.conf', 'crl-verify /etc/openvpn/server/old-crl.pem\n')
        with patch.object(self.backend, 'command', side_effect=command), \
                patch.object(self.backend, 'management', return_value=['END']):
            self.invalid('openvpn.apply', {'version': 1, 'device_id': 7, 'action': 'revoke',
                                           'ip': '10.67.67.2'}, 'unavailable')

    def test_openvpn_delete_interrupted_after_index_retry_publishes_crl(self):
        # Partial delete: the CA index says revoked, the CRL was never written.
        command, failures = self.openvpn_ca()
        failures.append('-gencrl')
        params = {'version': 1, 'device_id': 7, 'action': 'revoke', 'ip': '10.67.67.2'}
        with patch.object(self.backend, 'command', side_effect=command), \
                patch.object(self.backend, 'management', return_value=['END']):
            self.invalid('openvpn.apply', params, 'unavailable')
            self.assertEqual(self.backend.certificates(7), [('R', '0A')])
            failures.clear()
            reply = self.request('openvpn.apply', params)
        self.assertTrue(reply['ok'], reply)
        self.assertTrue(reply['result']['observed']['revoked'])

    def test_wg_failed_apply_is_not_confirmed_and_retry_completes(self):
        path = self.file('/etc/wireguard/wg0.conf', '[Interface]\nPrivateKey = server-secret\n'
                         '\n[Peer]\nPublicKey = ' + self.public + '\n')
        before = path.read_text()
        kernel = {self.public: '10.66.66.2/32 fd66:66::2/128'}
        broken = [True]

        def command(*args, **_options):
            if args[1] == 'syncconf':
                if broken[0]:
                    raise self.contract.Fault('unavailable')
                kernel.clear()
                return ''
            if args[-1] == 'preshared-keys':
                return ''
            return ''.join(key + '\t' + value + '\n' for key, value in kernel.items())

        params = {'version': 1, 'peers': []}
        with patch.object(self.backend, 'command', side_effect=command):
            self.invalid('wg.apply', params, 'unavailable')
            self.assertEqual(path.read_text(), before)
            self.assertIn(self.public, kernel)
            broken[0] = False
            self.assertEqual(self.request('wg.apply', params)['result']['observed'], {'peers': {}})
        self.assertNotIn(self.public, path.read_text())

    def test_wg_peer_left_in_kernel_is_not_confirmed(self):
        self.file('/etc/wireguard/wg0.conf', '[Interface]\nPrivateKey = server-secret\n')

        def command(*args, **_options):
            if args[1] == 'syncconf':
                return ''
            if args[-1] == 'preshared-keys':
                return ''
            return self.public + '\t10.66.66.2/32 fd66:66::2/128\n'

        with patch.object(self.backend, 'command', side_effect=command):
            self.invalid('wg.apply', {'version': 1, 'peers': []}, 'unavailable')

    def ikev2_world(self):
        self.file('/etc/swanctl/conf.d/anysda-clients.conf', '')
        world = {'live': True, 'fail': None, 'terminate': True}

        def command(*args, **_options):
            if world['fail'] and world['fail'] in args:
                raise self.contract.Fault('unavailable')
            if '--list-sas' in args:
                return "anysda-dev-7: #42, ESTABLISHED\n  remote EAP: 'device7'\n" if world['live'] else ''
            if '--terminate' in args and world['terminate']:
                world['live'] = False
            return ''
        return command, world

    def test_ikev2_failed_load_is_not_confirmed_and_retry_terminates(self):
        command, world = self.ikev2_world()
        world['fail'] = '--load-creds'
        params = {'version': 1, 'clients': []}
        with patch.object(self.backend, 'command', side_effect=command):
            self.invalid('ikev2.apply', params, 'unavailable')
            self.assertTrue(world['live'])
            self.assertEqual(json.loads((self.state / 'ikev2-pending.json').read_text()), ['device7'])
            world['fail'] = None
            self.assertTrue(self.request('ikev2.apply', params)['ok'])
        self.assertFalse(world['live'])

    def test_ikev2_sa_that_survives_terminate_is_not_confirmed(self):
        command, world = self.ikev2_world()
        world['terminate'] = False
        with patch.object(self.backend, 'command', side_effect=command):
            self.invalid('ikev2.apply', {'version': 1, 'clients': []}, 'unavailable')
        self.assertEqual(json.loads((self.state / 'ikev2-pending.json').read_text()), ['device7'])

    def test_ikev2_delete_interrupted_at_terminate_retry_finishes(self):
        command, world = self.ikev2_world()
        world['fail'] = '--terminate'
        params = {'version': 1, 'clients': []}
        with patch.object(self.backend, 'command', side_effect=command):
            self.invalid('ikev2.apply', params, 'unavailable')
            # A fresh process after the failure still knows whom to terminate.
            self.backend = self.backend_module.Backend(self.root, self.state)
            self.backend.pools = self.pools
            world['fail'] = None
        with patch.object(self.backend, 'command', side_effect=command):
            self.assertTrue(self.request('ikev2.apply', params)['ok'])
        self.assertFalse(world['live'])

    def test_ikev2_numeric_sa_and_persistent_retry(self):
        self.file('/etc/swanctl/conf.d/anysda-clients.conf', '')
        calls = []
        live = [True]

        def command(*args):
            calls.append(args)
            if '--list-sas' in args:
                return "anysda-dev-7: #42, ESTABLISHED\n  remote EAP: 'device7'\n" if live[0] else ''
            if '--terminate' in args:
                live[0] = False
            return ''

        with patch.object(self.backend, 'command', side_effect=command):
            reply = self.request('ikev2.apply', {'version': 1, 'clients': []})
        self.assertTrue(reply['ok'], reply)
        self.assertIn(('/usr/sbin/swanctl', '--terminate', '--ike-id', '42'), calls)
        self.assertIn(('/usr/sbin/swanctl', '--load-creds', '--clear', '--noprompt'), calls)
        self.assertEqual(json.loads((self.state / 'ikev2-pending.json').read_text()), [])

    def test_ikev2_sa_without_separate_eap_id_is_terminated(self):
        # Клиент, у которого IKE-идентификатор равен логину: swanctl не печатает «EAP: '...'».
        self.file('/etc/swanctl/conf.d/anysda-clients.conf', '')
        calls = []
        live = [True]

        def command(*args):
            calls.append(args)
            if '--list-sas' in args:
                return ("anysda-dev-7: #42, ESTABLISHED, IKEv2, 1_i 2_r*\n"
                        "  remote 'device7' @ 198.51.100.7[4500] [10.68.68.2]\n") if live[0] else ''
            if '--terminate' in args:
                live[0] = False
            return ''

        with patch.object(self.backend, 'command', side_effect=command):
            reply = self.request('ikev2.apply', {'version': 1, 'clients': []})
        self.assertTrue(reply['ok'], reply)
        self.assertIn(('/usr/sbin/swanctl', '--terminate', '--ike-id', '42'), calls)

    def test_ikev2_rejects_config_injection(self):
        client = {'device_id': 1, 'username': 'test', 'password': 'valid-password-123', 'ip': '10.68.68.2'}
        for name, value in [('username', 'evil\ninclude /etc/shadow'), ('password', 'x" } secrets {')]:
            self.invalid('ikev2.apply', {'version': 1, 'clients': [dict(client, **{name: value})]})

    def test_legacy_transliterated_ikev2_username_is_preserved(self):
        # Two 64-character names can expand threefold during transliteration,
        # followed by a separator and the existing uniqueness suffix.
        client = {'device_id': 1, 'username': 'sch' * 64 + '-' + 'sch' * 64 + '-999',
                  'password': 'valid-password-123', 'ip': '10.68.68.2'}
        with patch.object(self.backend, 'ikev2', return_value={}) as apply:
            self.assertTrue(self.request('ikev2.apply', {'version': 1, 'clients': [client]})['ok'])
        self.assertEqual(apply.call_args.args[0]['clients'][0]['username'], client['username'])
        self.invalid('ikev2.apply', {'version': 2, 'clients': [dict(client, username='a' * 513)]})

    def test_config_routes_to_same_adapter_and_state_is_live(self):
        with patch.object(self.backend, 'wireguard', return_value={'peers': {}}) as apply:
            self.assertTrue(self.request('config.apply', {'resource': 'wg-peers', 'schema': 1,
                                                          'version': 1, 'config': {'peers': []}})['ok'])
            apply.assert_called_once_with({'version': 1, 'peers': []})
        with patch.object(self.backend, 'observe', side_effect=OSError('private-key')):
            self.invalid('state.read', {'protocol': 'wg'}, 'unavailable')
            self.invalid('check.run', {'check': 'wg'}, 'unavailable')

    # Expiry far ahead of the real clock; the timer runs with a later clock.
    EXPIRES = '2030-01-01T00:00:00.000Z'
    EXTENDED = '2031-01-01T00:00:00.000Z'
    LATER = '2030-01-01T00:00:01.000Z'

    def later(self):
        return patch.object(self.backend_module, 'utc_now', return_value=self.LATER)

    def wg_kernel(self):
        self.file('/etc/wireguard/wg0.conf', '[Interface]\nPrivateKey = server-secret\nListenPort = 51820\n')
        kernel = {}

        def command(*args, data=None):
            if args[1] == 'syncconf':
                kernel.clear()
                for block in data.split('[Peer]')[1:]:
                    values = dict(line.split(' = ', 1) for line in block.strip().splitlines())
                    kernel[values['PublicKey']] = (values['PresharedKey'], values['AllowedIPs'].split(', '))
            elif args[-1] == 'preshared-keys':
                return ''.join(f'{name}\t{psk}\n' for name, (psk, _ips) in kernel.items())
            elif args[-1] == 'allowed-ips':
                return ''.join(f'{name}\t{" ".join(ips)}\n' for name, (_psk, ips) in kernel.items())
            return ''
        return kernel, command

    def test_expired_wg_peer_is_removed_without_the_panel(self):
        kernel, command = self.wg_kernel()
        other = {'key': base64.b64encode(bytes(range(2, 34))).decode(), 'preshared_key': self.psk,
                 'ip': '10.66.66.3'}
        expiring = dict(self.peer, expires_at=self.EXPIRES)
        with patch.object(self.backend, 'command', side_effect=command):
            self.assertTrue(self.request('wg.apply', {'version': 1, 'peers': [expiring, other]})['ok'])
            self.assertEqual(self.request('state.read', {'protocol': 'wg'})['result']['expires'],
                             {self.public: self.EXPIRES})
            with self.later():
                result = self.request('expiry.run', {})['result']
                self.assertEqual(result['latest'], [{'protocol': 'wg', 'id': self.public,
                                                     'expires_at': self.EXPIRES}])
                self.assertEqual(set(kernel), {other['key']})
                self.assertNotIn(self.public, (self.root / 'etc/wireguard/wg0.conf').read_text())
                # Reported once; a panel retry of the same version does not return access.
                self.assertEqual(self.request('expiry.run', {})['result']['latest'], [])
                self.assertTrue(self.request('wg.apply', {'version': 1, 'peers': [expiring, other]})['ok'])
                self.assertEqual(set(kernel), {other['key']})
                self.assertEqual(len(self.request('expiry.run', {})['result']['removed']), 1)
                # The panel stays the source of truth: a later expiry restores access.
                extended = dict(self.peer, expires_at=self.EXTENDED)
                self.assertTrue(self.request('wg.apply', {'version': 2, 'peers': [extended, other]})['ok'])
                self.assertEqual(self.request('expiry.run', {})['result']['latest'], [])
                self.assertEqual(set(kernel), {self.public, other['key']})
        self.invalid('wg.apply', {'version': 3, 'peers': [dict(self.peer, expires_at='2030-01-01')]})

    def test_extension_before_expiry_keeps_access(self):
        kernel, command = self.wg_kernel()
        with patch.object(self.backend, 'command', side_effect=command):
            self.assertTrue(self.request('wg.apply', {'version': 1, 'peers': [
                dict(self.peer, expires_at=self.EXPIRES)]})['ok'])
            self.assertTrue(self.request('wg.apply', {'version': 2, 'peers': [
                dict(self.peer, expires_at=self.EXTENDED)]})['ok'])
            with self.later():
                self.assertEqual(self.request('expiry.run', {})['result']['latest'], [])
        self.assertEqual(set(kernel), {self.public})

    def test_expired_openvpn_device_is_denied_and_dropped_without_the_panel(self):
        self.file('/etc/openvpn/server/pki/index.txt', 'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        self.file('/etc/openvpn/server/ccd/dev_7', '')
        sessions = {'dev_7': '10.67.67.2'}

        def management(command):
            if command == 'kill dev_7':
                sessions.clear()
                return ['SUCCESS: killed']
            return [f'CLIENT_LIST,{name},192.0.2.2:1,{ip}' for name, ip in sessions.items()] + ['END']

        params = {'version': 1, 'device_id': 7, 'action': 'allow', 'ip': '10.67.67.2',
                  'expires_at': self.EXPIRES}
        ccd = self.root / 'etc/openvpn/server/ccd/dev_7'
        with patch.object(self.backend, 'command', return_value=''), \
                patch.object(self.backend, 'management', side_effect=management):
            self.assertTrue(self.request('openvpn.apply', params)['ok'])
            self.assertNotIn('disable', ccd.read_text())
            self.assertEqual(self.request('state.read', {'protocol': 'openvpn'})['result']['expires'],
                             {'7': self.EXPIRES})
            with self.later():
                result = self.request('expiry.run', {})['result']
                self.assertEqual(result['latest'], [{'protocol': 'openvpn', 'id': 7,
                                                     'expires_at': self.EXPIRES}])
                self.assertIn('disable', ccd.read_text())
                self.assertEqual(sessions, {})
                self.assertEqual(self.request('expiry.run', {})['result']['latest'], [])
                # The client retries after the drop: cut again, not a second removal.
                sessions['dev_7'] = '10.67.67.2'
                result = self.request('expiry.run', {})['result']
                self.assertEqual((result['latest'], len(result['removed']), sessions), ([], 1, {}))
                reply = self.request('openvpn.apply', params)
                self.assertFalse(reply['result']['observed']['allowed'])
                self.assertIn('disable', ccd.read_text())
                extended = dict(params, version=2, expires_at=self.EXTENDED)
                self.assertTrue(self.request('openvpn.apply', extended)['ok'])
                self.assertNotIn('disable', ccd.read_text())

    def test_expired_ikev2_client_is_removed_without_the_panel(self):
        self.file('/etc/swanctl/conf.d/anysda-clients.conf', '')
        sas = {}

        def command(*args):
            if '--list-conns' in args:
                text = (self.root / 'etc/swanctl/conf.d/anysda-clients.conf').read_text()
                return ''.join(f'anysda-dev-{value}: IKEv2\n' for value in
                               re.findall(r'^  anysda-dev-(\d+) : connections', text, re.M))
            if '--list-sas' in args:
                return ''.join(f"anysda-dev-{number}: #{number}, ESTABLISHED\n  remote EAP: '{name}'\n"
                               for number, name in sas.items())
            if '--terminate' in args:
                sas.pop(int(args[-1]))
            return ''

        expiring = {'device_id': 7, 'username': 'device7', 'password': 'valid-password-007',
                    'ip': '10.68.68.2', 'expires_at': self.EXPIRES}
        other = {'device_id': 8, 'username': 'device8', 'password': 'valid-password-008',
                 'ip': '10.68.68.3', 'dns': ['10.99.0.1'], 'expires_at': None}
        with patch.object(self.backend, 'command', side_effect=command):
            self.assertTrue(self.request('ikev2.apply', {'version': 1, 'clients': [expiring, other]})['ok'])
            sas.update({7: 'device7', 8: 'device8'})
            state = self.request('state.read', {'protocol': 'ikev2'})['result']
            self.assertEqual(state['expires'], {'7': self.EXPIRES})
            # The panel's credential digest does not include the expiry.
            plain = {name: value for name, value in other.items() if name != 'expires_at'}
            digest = hashlib.sha256(json.dumps(plain, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.assertEqual(state['credentials']['device8'], digest)
            with self.later():
                result = self.request('expiry.run', {})['result']
                self.assertEqual(result['latest'], [{'protocol': 'ikev2', 'id': 7,
                                                     'expires_at': self.EXPIRES}])
                state = self.request('state.read', {'protocol': 'ikev2'})['result']
                self.assertEqual(state['devices'], [8])
                self.assertEqual(sas, {8: 'device8'})
                self.assertEqual(state['credentials']['device8'], digest)
                self.assertEqual(self.request('expiry.run', {})['result']['latest'], [])
                # The client retries after the drop: cut again, not a second removal.
                sas[7] = 'device7'
                result = self.request('expiry.run', {})['result']
                self.assertEqual((result['latest'], len(result['removed']), sas), ([], 1, {8: 'device8'}))

    def test_deploy_runs_the_expiry_timer(self):
        stage = (ROOT / 'infra/scripts/29-control.sh').read_text()
        self.assertIn('systemctl enable --now vpn2-control-expiry.timer', stage)
        timer = (CONTROL / 'vpn2-control-expiry.timer').read_text()
        self.assertIn('OnUnitActiveSec=30s', timer)
        service = (CONTROL / 'vpn2-control-expiry.service').read_text()
        self.assertIn('launcher.py --expire', service)
        # A restore stops the socket; the timer must not start it again.
        self.assertNotIn('Requires=vpn2-control.socket', service)

    def test_bounded_command_output_and_timeout(self):
        with self.assertRaises(self.contract.Fault):
            self.backend_module.run([sys.executable, '-c', 'print("x" * 1100000)'], time.monotonic() + 2)
        started = time.monotonic()
        with self.assertRaises(self.contract.Fault):
            self.backend_module.run([sys.executable, '-c', 'import time; time.sleep(10)'],
                                    time.monotonic() + 0.1)
        self.assertLess(time.monotonic() - started, 2)

    def test_deploy_installs_service_in_pipeline(self):
        deploy = (ROOT / 'infra/deploy.sh').read_text()
        self.assertIn('run_stage 29-control', deploy)
        stage = (ROOT / 'infra/scripts/29-control.sh').read_text()
        self.assertIn('systemctl enable --now vpn2-control.socket', stage)
        self.assertIn('install -o root -g root -m 0644', stage)
        service = (CONTROL / 'vpn2-control.service').read_text()
        self.assertIn('User=vpn2-control', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertNotIn('CAP_SYS_ADMIN', service)

    def test_panel_api_requirements_and_public_profile_boundary(self):
        self.file('/etc/wireguard/server.pub', self.public + '\n')
        self.file('/etc/wireguard/server.priv', 'private-server-key')
        reply = self.request('profile.read', {'protocol': 'wg'})
        self.assertEqual(reply, {'api': 1, 'ok': True, 'result': {'publicKey': self.public}})
        description = self.request('api.describe', {})
        self.assertTrue(description['ok'])
        for method in ('openvpn.issue', 'openvpn.revoke', 'traffic.read', 'settings.apply'):
            self.assertIn(method, description['result']['methods'])
        self.invalid('profile.read', {'protocol': 'wg', 'path': '/etc/wireguard/server.priv'})
        self.invalid('profile.read', {'protocol': 'ca-key'})

    def test_settings_validate_before_write_and_preserve_unrelated_files(self):
        path = self.file('/etc/anysda/manual-routes.json', '[]\n')
        secret = self.file('/etc/anysda/clash-secret.txt', 'unrelated-secret')
        params = {'resource': 'routes', 'version': 1,
                  'config': [{'type': 'domain', 'value': 'example.org', 'outbound': 'direct-ru'}]}
        self.assertTrue(self.request('settings.apply', params)['ok'])
        self.assertEqual(json.loads(path.read_text()), params['config'])
        before = path.read_bytes()
        for config in ([{'type': 'domain', 'value': 'x\ninclude /etc/shadow', 'outbound': 'direct-ru'}],
                       [{'type': 'ip_cidr', 'value': '999.1.1.1', 'outbound': 'direct-ru'}]):
            self.invalid('settings.apply', dict(params, config=config, version=2))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(secret.read_text(), 'unrelated-secret')
        self.invalid('settings.apply', dict(params, version=1, config=[]), 'conflict')
        self.invalid('settings.apply', dict(params, resource='../../etc/shadow'))
        self.invalid('settings.apply', {'resource': 'preferences', 'version': 1,
                                       'config': {'exit': ['127.0.0.1']}})

    def test_certificate_api_cannot_revoke_another_device_or_accept_paths(self):
        self.file('/etc/openvpn/server/pki/index.txt',
                  'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        self.invalid('openvpn.revoke', {'device_id': 8, 'serial': '0A'})
        self.invalid('openvpn.revoke', {'device_id': 7, 'serial': '../../ca.key'})
        self.invalid('openvpn.issue', {'device_id': 7, 'generation': 'x\ncommand'})
        self.invalid('openvpn.disconnect', {'device_id': '7\nkill all'})

    def test_issue_retry_returns_same_key_and_certificate_after_restart(self):
        self.file('/etc/openvpn/server/pki/index.txt', '')
        calls = []

        def command(*args):
            calls.append(args)
            if 'genpkey' in args:
                Path(args[-1]).write_text('device-key')
            elif '-pubout' in args:
                return 'public'
            elif '-pubkey' in args:
                return 'public'
            elif 'req' in args:
                Path(args[args.index('-out') + 1]).write_text('request')
            elif '-serial' in args:
                return 'serial=0A\n'
            elif 'ca' in args:
                Path(args[-1]).write_text('certificate')
                self.file('/etc/openvpn/server/pki/newcerts/0A.pem', 'certificate')
                self.file('/etc/openvpn/server/pki/index.txt',
                          'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
            return ''

        params = {'device_id': 7, 'generation': 'a' * 64}
        with patch.object(self.backend, 'command', side_effect=command):
            first = self.request('openvpn.issue', params)
        self.assertTrue(first['ok'], first)
        # Simulate death after CA signing but before publication of the reply.
        (self.state / ('issue-7-' + 'a' * 64) / 'cert.pem').unlink()
        self.backend = self.backend_module.Backend(self.root, self.state)
        self.backend.pools = self.pools
        with patch.object(self.backend, 'command', side_effect=command):
            second = self.request('openvpn.issue', params)
        self.assertEqual(first, second)
        self.assertEqual(sum('genpkey' in args for args in calls), 1)
        self.assertEqual(sum('ca' in args for args in calls), 1)

    def test_missing_ccd_is_not_a_confirmed_deny(self):
        self.file('/etc/openvpn/server/pki/index.txt',
                  'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        with patch.object(self.backend, 'command', return_value=''):
            result = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['result']['present'])

    def test_management_uid_switch_is_transactional_and_survives_redeploy(self):
        panel = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        stage = (ROOT / 'infra/scripts/29-openvpn.sh').read_text()
        self.assertIn('management-client-user ${OVPN_CONTROL_USER}', stage)
        self.assertIn('OVPN_CONTROL_USER=vpn2-control', stage)
        switch = panel.index("sed -i 's/^management-client-user")
        guard = panel.index('panel_guard_files /etc/caddy')
        self.assertLess(panel.index('\npanel_begin'), guard)
        self.assertLess(guard, switch)
        self.assertIn('/etc/openvpn/server/server.conf', panel[guard:switch])
        self.assertIn('systemctl try-restart openvpn-server@server', panel)
        self.assertIn('pwd.getpwnam(\'vpn2-control\').pw_uid',
                      (ROOT / 'infra/lib/verify-traffic.py').read_text())

    def test_no_system_execution_or_writable_config_mounts_in_panel(self):
        for path in (ROOT / 'app/server').rglob('*.ts'):
            self.assertNotIn('node:child_process', path.read_text(), str(path))
        panel = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        bot = (ROOT / 'infra/scripts/35-telegram.sh').read_text()
        for stage in (panel, bot):
            self.assertIn('--user ', stage)
            self.assertIn('--cap-drop ALL', stage)
            self.assertNotIn('--cap-add', stage)
            self.assertNotIn('--pid host', stage)
            self.assertNotIn('docker.sock', stage)
        for directory in ('wireguard', 'openvpn', 'strongswan', 'swanctl'):
            self.assertNotIn('-v /etc/' + directory, panel)
        self.assertNotIn('-v /etc/anysda:/etc/anysda', panel)
        self.assertLess(panel.index('scripts/control-probe.mjs'), panel.index('\npanel_begin'))
        socket_unit = (CONTROL / 'vpn2-control.socket').read_text()
        self.assertIn('PartOf=vpn2-control.service', socket_unit)

    def test_image_probe_requires_every_method_the_panel_calls(self):
        # A panel on an older service must stop at the probe, not fail its applies.
        probe = (ROOT / 'app/scripts/control-probe.mjs').read_text()
        required = set(re.findall(r"'([a-z0-9]+\.[a-z]+)'", probe.split(']')[0]))
        called = set()
        for path in (ROOT / 'app/server').rglob('*.ts'):
            called |= set(re.findall(r"control(?:Apply)?(?:<[^>(]*>)?\('([a-z0-9]+\.[a-z]+)'", path.read_text()))
        self.assertIn('wg.apply', called)
        self.assertEqual(called - required, set())


if __name__ == '__main__':
    unittest.main()
