"""Удалённое устройство не оставляет на entry своих файлов.

CA настоящий (openssl), сервер OpenVPN подменён ответом управляющего сокета.
Панель при удалении зовёт openvpn.revoke и openvpn.apply с deny, читает
openvpn.certificate и стирает строку; больше про устройство служба не слышит.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from vpn2 import pki
from vpn2.control import backend as backend_module
from vpn2.control.contract import Fault

EMPTY = hashlib.sha256(b'').hexdigest()
OLD = 3600


def openssl(*argv, data=None):
    return subprocess.run(['openssl', *argv], input=data, check=True, capture_output=True, text=True).stdout


class Entry:
    def __init__(self, root):
        self.root = root
        self.state = root / 'state'
        self.base = root / 'etc/openvpn/server'
        self.ca = self.base / 'pki'
        (self.ca / 'newcerts').mkdir(parents=True)
        (self.base / 'ccd').mkdir()
        self.state.mkdir()
        (self.ca / 'openssl.cnf').write_text(pki.openssl_cnf(self.ca))
        (self.ca / 'index.txt').write_text('')
        (self.ca / 'serial').write_text('01\n')
        (self.ca / 'crlnumber').write_text('01\n')
        openssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                '-out', str(self.ca / 'ca.key'))
        openssl('req', '-x509', '-new', '-key', str(self.ca / 'ca.key'), '-config', str(self.ca / 'openssl.cnf'),
                '-extensions', 'v3_ca', '-days', '30', '-out', str(self.ca / 'ca.crt'))
        pki.gencrl(self.ca, self.ca / 'crl.pem')
        (self.base / 'server.conf').write_text('crl-verify /etc/openvpn/server/pki/crl.pem\n')
        self.backend = backend_module.Backend(root, self.state)
        self.backend.pools = json.loads((Path(__file__).parents[3] / 'app/shared/vpn-pools.json').read_text())
        self.sessions = []
        self.stopped = False
        self.backend.management = self.management
        self.version = 0

    def management(self, command):
        if self.stopped:
            raise Fault('unavailable')
        if command.startswith('kill '):
            self.sessions.remove(command[5:])
            return ['SUCCESS']
        return [f'CLIENT_LIST,{cn},192.0.2.2:1,10.67.67.2' for cn in self.sessions] + ['END']

    def call(self, method, **params):
        return self.backend.execute(method, params)

    def apply(self, device, action):
        self.version += 1
        return self.call('openvpn.apply', version=self.version, device_id=device, action=action,
                         ip=f'10.67.67.{device}')

    def issue(self, device, previous=''):
        generation = hashlib.sha256(previous.encode()).hexdigest()
        return self.call('openvpn.issue', device_id=device, generation=generation)['cert']

    def serial(self, cert):
        return openssl('x509', '-noout', '-serial', data=cert).strip().partition('=')[2]

    def create(self, device):
        cert = self.issue(device)
        self.apply(device, 'allow')
        return cert

    def delete(self, device, cert):
        """Вызовы панели при удалении устройства, в её порядке."""
        self.call('openvpn.revoke', device_id=device, serial=self.serial(cert))
        self.apply(device, 'deny')
        return self.call('openvpn.certificate', device_id=device, serial=self.serial(cert))

    def age(self, seconds=OLD):
        stamp = time.time() - seconds
        for path in [*self.state.rglob('*'), *(self.base / 'ccd').iterdir()]:
            os.utime(path, (stamp, stamp))

    def timer(self):
        return self.call('expiry.run')

    def files(self, device):
        found = [path.name for path in self.state.iterdir() if re.match(rf'(openvpn|issue)-{device}\b', path.name)]
        found += [path.name for path in (self.base / 'ccd').iterdir() if path.name == f'dev_{device}']
        return sorted(re.sub(r'-[a-f0-9]{64}$', '-<hash>', name) for name in found)

    def refused(self, cert):
        """Как сервер: сертификат против CA и опубликованного CRL."""
        path = self.root / 'presented.pem'
        path.write_text(cert)
        return not pki.verify(self.ca / 'ca.crt', path, crl=self.ca / 'crl.pem')


@pytest.fixture
def entry(tmp_path):
    previous = os.umask(0o022)
    try:
        yield Entry(tmp_path)
    finally:
        os.umask(previous)


FULL = ['dev_7', 'issue-7-<hash>', 'openvpn-7.json']


def test_deleted_device_leaves_no_files(entry):
    cert = entry.create(7)
    assert entry.files(7) == FULL
    seen = entry.delete(7, cert)
    # Панель подтверждает удаление по этому ответу: сразу за ним ничего не пропадает.
    assert (seen['present'], seen['revoked'], seen['allowed']) == (True, True, False)
    entry.timer()
    assert entry.files(7) == FULL
    entry.age()
    entry.timer()
    assert entry.files(7) == []


def test_revoked_certificate_is_still_refused_after_the_cleanup(entry):
    cert = entry.create(7)
    assert not entry.refused(cert)
    entry.delete(7, cert)
    entry.age()
    entry.timer()
    assert entry.files(7) == []
    assert entry.refused(cert)
    serial = entry.serial(cert)
    seen = entry.call('openvpn.certificate', device_id=7, serial=serial)
    assert (seen['revoked'], seen['valid'], seen['allowed']) == (True, False, False)
    # Отзыв живёт в базе CA: следующий CRL строится из неё же.
    assert entry.backend.certificates(7) == [('R', serial)]
    assert (entry.ca / 'newcerts' / (serial + '.pem')).is_file()
    other = entry.create(8)
    entry.delete(8, other)
    assert entry.refused(cert) and entry.refused(other)
    with pytest.raises(Fault):
        entry.apply(7, 'allow')


def test_deletion_while_the_server_is_stopped(entry):
    cert = entry.create(7)
    entry.stopped = True
    entry.call('openvpn.revoke', device_id=7, serial=entry.serial(cert))
    with pytest.raises(Fault):
        entry.apply(7, 'deny')
    entry.age()
    entry.timer()
    assert entry.files(7) == []
    assert entry.refused(cert)
    # Сервер поднялся, панель повторяет удаление: подтверждается и снова прибирается.
    entry.stopped = False
    entry.version -= 1
    entry.apply(7, 'deny')
    serial = entry.serial(cert)
    assert entry.call('openvpn.certificate', device_id=7, serial=serial)['present'] is True
    entry.age()
    entry.timer()
    assert entry.files(7) == []


def test_restored_leftovers_of_devices_deleted_long_ago_are_removed(entry):
    gone = entry.create(7)
    entry.create(8)
    entry.delete(7, gone)
    entry.age(30 * 86400)
    # Узел восстановлен из архива, служба только что поднята заново.
    entry.backend = backend_module.Backend(entry.root, entry.state)
    entry.backend.management = entry.management
    entry.timer()
    assert entry.files(7) == []
    assert entry.files(8) == ['dev_8', 'issue-8-<hash>', 'openvpn-8.json']
    assert entry.refused(gone)


def test_living_devices_keep_their_files(entry):
    allowed = entry.create(7)
    entry.create(8)
    entry.apply(8, 'deny')
    entry.sessions.append('dev_7')
    entry.age(30 * 86400)
    before = {path: path.read_bytes() for path in [*entry.state.rglob('*'), *(entry.base / 'ccd').iterdir()]
              if path.is_file()}
    entry.timer()
    assert {path: path.read_bytes() for path in before} == before
    assert not entry.refused(allowed) and entry.sessions == ['dev_7']
    assert entry.call('state.read', protocol='openvpn')['sessions'] == {'dev_7': '10.67.67.2'}


def test_reissue_drops_the_journal_of_the_revoked_certificate_only(entry):
    first = entry.create(7)
    entry.call('openvpn.revoke', device_id=7, serial=entry.serial(first))
    second = entry.issue(7, first)
    entry.apply(7, 'allow')
    entry.age()
    entry.timer()
    kept = [path for path in entry.state.iterdir() if path.name.startswith('issue-7-')]
    assert [path.name for path in kept] == ['issue-7-' + hashlib.sha256(first.encode()).hexdigest()]
    assert (kept[0] / 'cert.pem').read_text() == second
    assert (entry.base / 'ccd/dev_7').is_file() and (entry.state / 'openvpn-7.json').is_file()
    assert entry.refused(first) and not entry.refused(second)
    # Повтор выпуска панелью после потерянного ответа отдаёт тот же сертификат.
    assert entry.issue(7, first) == second


def test_issue_in_progress_is_not_touched(entry):
    journal = entry.state / ('issue-9-' + EMPTY)
    journal.mkdir()
    (journal / 'key.pem').write_text('key')
    cert = entry.create(7)
    entry.call('openvpn.revoke', device_id=7, serial=entry.serial(cert))
    half = entry.state / ('issue-7-' + hashlib.sha256(cert.encode()).hexdigest())
    half.mkdir()
    (half / 'key.pem').write_text('key')
    entry.timer()
    assert journal.is_dir() and half.is_dir()


def test_cleanup_failure_does_not_stop_the_expiry(entry):
    cert = entry.create(7)
    entry.delete(7, cert)
    entry.age()
    (entry.ca / 'index.txt').write_text('garbage\n')
    assert entry.timer()['failed'] == []
    assert entry.files(7) == FULL


def traces(entry, *needles):
    files = [path for path in [*entry.state.rglob('*'), *(entry.root / 'etc').rglob('*')] if path.is_file()]
    return sorted(path.name for path in files if any(needle in path.read_text() for needle in needles))


def test_deleted_wireguard_device_leaves_no_trace(entry):
    conf = entry.root / 'etc/wireguard/wg0.conf'
    conf.parent.mkdir(parents=True)
    conf.write_text('[Interface]\nPrivateKey = server-secret\nListenPort = 51820\n')
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

    entry.backend.command = command
    psk = base64.b64encode(bytes(range(1, 33))).decode()
    gone, kept = ({'key': base64.b64encode(bytes(range(start, start + 32))).decode(), 'preshared_key': psk,
                   'ip': f'10.66.66.{start + 2}'} for start in (0, 2))
    entry.call('wg.apply', version=1, peers=[gone, kept])
    assert traces(entry, gone['key']) == ['wg.json', 'wg0.conf']
    entry.call('wg.apply', version=2, peers=[kept])
    assert traces(entry, gone['key'], gone['ip'] + '/') == [] and set(kernel) == {kept['key']}


def test_deleted_ikev2_device_leaves_no_trace(entry):
    conf = entry.root / 'etc/swanctl/conf.d/anysda-clients.conf'
    conf.parent.mkdir(parents=True)
    conf.write_text('')

    def command(*args, **_options):
        if '--list-conns' in args:
            return ''.join(f'anysda-dev-{value}: IKEv2\n' for value in
                           re.findall(r'^  anysda-dev-(\d+) : connections', conf.read_text(), re.M))
        return ''

    entry.backend.command = command
    gone, kept = ({'device_id': number, 'username': f'device{number}', 'password': f'valid-password-00{number}',
                   'ip': f'10.68.68.{number}'} for number in (7, 8))
    entry.call('ikev2.apply', version=1, clients=[gone, kept])
    assert 'ikev2.json' in traces(entry, 'device7')
    entry.call('ikev2.apply', version=2, clients=[kept])
    assert traces(entry, 'device7', 'valid-password-007', 'anysda-dev-7') == []
    assert entry.call('state.read', protocol='ikev2')['devices'] == [8]
