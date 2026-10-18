"""Destructive fixture for a disposable host only: VPN2_CONTROL_TEST=1 required.

Run after 29-control with wireguard-tools, OpenVPN, OpenSSL, charon-systemd,
strongswan-swanctl and libcharon-extra-plugins installed. This tests real VPN
daemons and the installed systemd boundary, not traffic through an exit.
"""
import base64
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import time


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=30).stdout


def write(path, body, mode=0o600):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    target.chmod(mode)


def request(method, params):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(35)
        connection.connect('/run/vpn2-control/control.sock')
        connection.sendall(json.dumps({'api': 1, 'method': method, 'params': params}).encode() + b'\n')
        return json.loads(connection.makefile('rb').readline())


def expect(method, params, error=None):
    reply = request(method, params)
    if error:
        assert reply == {'api': 1, 'ok': False, 'error': error}, reply
    else:
        assert reply['ok'], reply
    print(method + ': ' + (error or 'OK'), flush=True)
    return reply


def credentials_probe():
    # First prove filesystem rejection without group membership.
    try:
        request('check.run', {'check': 'readiness'})
    except PermissionError:
        print('foreign UID outside socket group: Permission denied')
    else:
        raise AssertionError('unauthorized socket connection')


def wait_session(connected):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        reply = request('state.read', {'protocol': 'openvpn'})
        assert reply['ok'], reply
        if ('dev_7' in reply['result']['sessions']) == connected:
            return
        time.sleep(0.2)
    raise AssertionError('OpenVPN session did not reach expected state')


def openvpn_sessions():
    write('/tmp/vpn2-svc/client.conf', '''client
dev tun-test
proto udp
remote 127.0.0.1 1194
ca /etc/openvpn/server/pki/ca.crt
cert /etc/openvpn/server/pki/client.crt
key /etc/openvpn/server/pki/client.key
route-nopull
ifconfig-noexec
route-noexec
connect-retry 1 1
resolv-retry 1
''')
    expect('openvpn.apply', {'version': 1, 'device_id': 7, 'action': 'allow', 'ip': '10.67.67.2'})
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['openvpn', '--config', '/tmp/vpn2-svc/client.conf'],
                                   stdout=log, stderr=log)
        try:
            wait_session(True)
            print('OpenVPN real client connected: OK')
            expect('openvpn.apply', {'version': 2, 'device_id': 7, 'action': 'deny', 'ip': '10.67.67.2'})
            wait_session(False)
            time.sleep(3)
            assert 'dev_7' not in request('state.read', {'protocol': 'openvpn'})['result']['sessions']
            print('OpenVPN freeze: live session terminated; reconnect denied')
            expect('openvpn.apply', {'version': 3, 'device_id': 7, 'action': 'allow', 'ip': '10.67.67.2'})
            # OpenVPN clients may exit on AUTH_FAILED; reconnect explicitly
            # with the same profile after the administrator unfreezes it.
            process.terminate()
            process.wait(timeout=5)
            process = subprocess.Popen(['openvpn', '--config', '/tmp/vpn2-svc/client.conf'],
                                       stdout=log, stderr=log)
            wait_session(True)
            print('OpenVPN unfreeze: original client reconnected')
            expect('openvpn.apply', {'version': 4, 'device_id': 7, 'action': 'revoke', 'ip': '10.67.67.2'})
            wait_session(False)
            time.sleep(3)
            assert 'dev_7' not in request('state.read', {'protocol': 'openvpn'})['result']['sessions']
            print('OpenVPN revoke: live session terminated; reconnect denied')
        finally:
            process.terminate()
            process.wait(timeout=5)


def prepare():
    command('systemctl', 'stop', 'vpn2-control.service', 'vpn2-control.socket')
    for name in ('wg.json', 'openvpn-7.json', 'ikev2.json', 'ikev2-pending.json', 'ikev2-identities.json'):
        (Path('/var/lib/vpn2-control') / name).unlink(missing_ok=True)
    if subprocess.run(['ip', 'link', 'show', 'wg0'], capture_output=True, timeout=5).returncode == 0:
        command('ip', 'link', 'delete', 'wg0')
    private = command('wg', 'genkey').strip()
    write('/etc/wireguard/wg0.conf', '[Interface]\nAddress = 10.66.66.1/24\n'
          'ListenPort = 51820\nPrivateKey = ' + private + '\n')
    command('ip', 'link', 'add', 'wg0', 'type', 'wireguard')
    command('ip', 'address', 'add', '10.66.66.1/24', 'dev', 'wg0')
    command('ip', 'link', 'set', 'wg0', 'up')
    base = Path('/etc/openvpn/server/pki')
    (base / 'newcerts').mkdir(parents=True, exist_ok=True)
    Path('/etc/openvpn/server/ccd').mkdir(exist_ok=True)
    write(base / 'index.txt', '')
    write(base / 'serial', '01\n')
    write(base / 'crlnumber', '01\n')
    write(base / 'openssl.cnf', '''[ca]
default_ca = CA_default
[CA_default]
dir = /etc/openvpn/server/pki
database = $dir/index.txt
new_certs_dir = $dir/newcerts
certificate = $dir/ca.crt
private_key = $dir/ca.key
serial = $dir/serial
crlnumber = $dir/crlnumber
default_md = sha256
default_days = 2
default_crl_days = 2
policy = names
[names]
commonName = supplied
''')
    command('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
            '-subj', '/CN=control-test-ca', '-keyout', str(base / 'ca.key'), '-out', str(base / 'ca.crt'))
    for name, cn in [('server', 'control-test'), ('client', 'dev_7')]:
        command('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes',
                '-subj', '/CN=' + cn, '-keyout', str(base / (name + '.key')),
                '-out', str(base / (name + '.csr')))
        command('openssl', 'ca', '-batch', '-notext', '-config', str(base / 'openssl.cnf'),
                '-in', str(base / (name + '.csr')), '-out', str(base / (name + '.crt')))
    command('openssl', 'ca', '-batch', '-config', str(base / 'openssl.cnf'),
            '-gencrl', '-out', str(base / 'crl.pem'))
    write('/etc/openvpn/server/control-test.conf', '''dev tun
server 10.67.67.0 255.255.255.0
topology subnet
proto udp
port 1194
tls-server
ca /etc/openvpn/server/pki/ca.crt
cert /etc/openvpn/server/pki/server.crt
key /etc/openvpn/server/pki/server.key
dh none
crl-verify /etc/openvpn/server/pki/crl.pem
management /etc/openvpn/server/mgmt.sock unix
client-config-dir /etc/openvpn/server/ccd
''')
    command('systemctl', 'restart', 'openvpn-server@control-test')
    for source, destination in [('server.crt', 'x509/server.crt'), ('server.key', 'private/server.key'),
                                ('ca.crt', 'x509ca/ca.crt')]:
        write('/etc/swanctl/' + destination, (base / source).read_text())
    write('/etc/swanctl/conf.d/control-test.conf', '''connections {
  anysda-ikev2 {
    version = 2
    local-server {
      auth = pubkey
      certs = server.crt
      id = control-test
    }
    remote-client {
      auth = eap-mschapv2
      eap_id = %any
    }
    children {
      net { local_ts = 0.0.0.0/0 }
    }
  }
}
pools {
  anysda-ikev2-pool { addrs = 10.68.68.0/24 }
  anysda-ikev2-pool6 { addrs = fd68:68::/64 }
}
''')
    command('systemctl', 'restart', 'strongswan')
    command('systemctl', 'start', 'vpn2-control.socket')


def main():
    if sys.argv[1:] == ['--foreign']:
        credentials_probe()
        return
    if sys.argv[1:] == ['--group-only']:
        expect('check.run', {'check': 'readiness'}, 'forbidden')
        return
    if sys.argv[1:] == ['--panel']:
        expect('check.run', {'check': 'readiness'})
        for path in Path('/usr/local/lib/vpn2-control').iterdir():
            assert not os.access(path, os.W_OK), path.name
        assert not os.access('/usr/local/lib/vpn2-control', os.W_OK)
        assert not os.access('/etc/vpn2-control/callers.json', os.W_OK)
        assert not os.access('/etc/openvpn/server/pki/ca.key', os.R_OK)
        print('panel: sources/templates/validation read-only; CA key unreadable')
        return
    assert os.getuid() == 0 and os.environ.get('VPN2_CONTROL_TEST') == '1'
    prepare()
    expect('check.run', {'check': 'readiness'})
    peer = {'key': base64.b64encode(bytes(range(32))).decode(),
            'preshared_key': base64.b64encode(bytes(range(1, 33))).decode(), 'ip': '10.66.66.2'}
    expect('wg.apply', {'version': 1, 'peers': [peer]})
    expect('state.read', {'protocol': 'wg'})
    expect('config.apply', {'resource': 'wg-peers', 'schema': 1, 'version': 2, 'config': {'peers': []}})
    assert not command('wg', 'show', 'wg0', 'peers').strip()
    print('WireGuard revoked peer absent in kernel: OK')
    openvpn_sessions()
    expect('openvpn.apply', {'version': 5, 'device_id': 7, 'action': 'allow', 'ip': '10.67.67.2'}, 'conflict')
    verification = subprocess.run(['openssl', 'verify', '-crl_check', '-CAfile',
                                  '/etc/openvpn/server/pki/ca.crt', '-CRLfile',
                                  '/etc/openvpn/server/pki/crl.pem', '/etc/openvpn/server/pki/client.crt'],
                                 capture_output=True, text=True, timeout=5)
    assert verification.returncode != 0 and 'certificate revoked' in verification.stderr
    print('OpenVPN client certificate: certificate revoked')
    client = {'device_id': 7, 'username': 'device7', 'password': 'test-password-123', 'ip': '10.68.68.2'}
    expect('ikev2.apply', {'version': 1, 'clients': [client]})
    expect('state.read', {'protocol': 'ikev2'})
    expect('config.apply', {'resource': 'ikev2-clients', 'schema': 1, 'version': 2, 'config': {'clients': []}})
    assert 'anysda-dev-7' not in command('swanctl', '--list-conns')
    print('IKEv2 removed connection absent in charon: OK')
    for protocol in ('wg', 'openvpn', 'ikev2'):
        expect('check.run', {'check': protocol})
    expect('config.apply', {'resource': '/etc/shadow', 'schema': 1, 'version': 1, 'config': {}}, 'validation')
    expect('state.read', {'protocol': 'wg', 'extra': True}, 'validation')
    expect('wg.apply', {'version': 3, 'peers': [], 'padding': 'x' * 1048576}, 'limit')
    command('systemctl', 'restart', 'vpn2-control.service')
    expect('wg.apply', {'version': 1, 'peers': [peer]}, 'stale')
    for user, flag in [('nobody', '--foreign'), ('vpn2-panel', '--panel')]:
        print(command('runuser', '-u', user, '--', 'python3', __file__, flag).strip())
    if subprocess.run(['id', 'control-test-stranger'], capture_output=True, timeout=5).returncode:
        command('useradd', '--system', '--groups', 'vpn2-control', 'control-test-stranger')
    print(command('runuser', '-u', 'control-test-stranger', '--', 'python3', __file__, '--group-only').strip())
    assert pwd.getpwnam('vpn2-control').pw_uid != 0
    print('live control API: PASS')


if __name__ == '__main__':
    main()
