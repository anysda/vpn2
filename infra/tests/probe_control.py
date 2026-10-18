"""Live control API fixture for a test host: VPN2_CONTROL_TEST=1 required.

Run on an installed entry (or after 29-control with wireguard-tools, OpenVPN,
OpenSSL and strongswan-starter). This tests real VPN daemons and the installed
systemd boundary, not traffic through an exit.

The fixture needs its own WG/OpenVPN/IKEv2 configuration. The installation's
directories are moved aside for the run and moved back afterwards, also on
failure; the probe then checks them byte for byte and restarts the services
that were running. An interrupted run is recovered on the next start.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path('/')
# Same unit as 27-ikev2.sh and 99-verify.sh. Ubuntu 26.04 also installs
# charon-systemd (strongswan.service), a second charon the product never runs.
STRONGSWAN = 'strongswan-starter'
PRESERVED = ('/etc/wireguard', '/etc/openvpn/server', '/etc/swanctl', '/var/lib/vpn2-control')
SAVED = '.vpn2-probe-saved'
STRANGER = 'control-test-stranger'


def at(path):
    return ROOT / str(path).lstrip('/')


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=30).stdout


def systemctl(*args):
    return subprocess.run(['systemctl', *args], text=True, capture_output=True, timeout=60)


def write(path, body, mode=0o600):
    target = at(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    target.chmod(mode)


def manifest():
    rows = []
    for top in PRESERVED:
        base = at(top)
        for path in sorted([base, *base.rglob('*')]) if base.exists() else []:
            info = path.lstat()
            row = [str(path.relative_to(ROOT)), oct(info.st_mode), info.st_uid, info.st_gid]
            if path.is_symlink():
                row.append(os.readlink(path))
            elif path.is_file():
                row.append(hashlib.sha256(path.read_bytes()).hexdigest())
            rows.append(row)
    return rows


def put_back():
    for top in PRESERVED:
        saved = Path(str(at(top)) + SAVED)
        if saved.exists():
            if at(top).exists():
                shutil.rmtree(at(top))
            saved.rename(at(top))


def runtime_units():
    return ['wg-quick@wg0.service', 'openvpn-server@server.service', STRONGSWAN + '.service',
            'vpn2-control.socket', 'vpn2-control.service']


def preflight():
    """Everything that can refuse the run, before anything is changed."""
    state = systemctl('show', '-p', 'LoadState', '--value', STRONGSWAN + '.service')
    if state.stdout.strip() != 'loaded':
        raise SystemExit(f'{STRONGSWAN}.service is not installed; nothing changed')


def preserve():
    if any(Path(str(at(top)) + SAVED).exists() for top in PRESERVED):
        print('interrupted probe run found: restoring installation first', flush=True)
        put_back()
    active = [unit for unit in runtime_units() if systemctl('is-active', '--quiet', unit).returncode == 0]
    # Stopped first: a running daemon may rewrite its files (status log, wg SaveConfig).
    systemctl('stop', 'vpn2-control.service', 'vpn2-control.socket',
              'openvpn-server@server.service', 'wg-quick@wg0.service')
    before = manifest()
    stranger = subprocess.run(['id', STRANGER], capture_output=True, timeout=5).returncode == 0
    for top in PRESERVED:
        if at(top).exists():
            info = at(top).stat()
            at(top).rename(str(at(top)) + SAVED)
            at(top).mkdir(mode=0o700)
            os.chown(at(top), info.st_uid, info.st_gid)
            at(top).chmod(info.st_mode & 0o7777)
    return {'active': active, 'manifest': before, 'stranger': stranger}


def restore(saved):
    systemctl('stop', 'vpn2-control.service', 'vpn2-control.socket', 'openvpn-server@control-test.service')
    if subprocess.run(['ip', 'link', 'show', 'wg0'], capture_output=True, timeout=5).returncode == 0:
        command('ip', 'link', 'delete', 'wg0')
    if not saved['stranger'] and subprocess.run(['id', STRANGER], capture_output=True, timeout=5).returncode == 0:
        command('userdel', STRANGER)
    shutil.rmtree(at('/tmp/vpn2-svc'), ignore_errors=True)
    put_back()
    after = manifest()
    assert after == saved['manifest'], 'installation files differ after restore'
    print(f'installation restored byte for byte: {len(after)} paths', flush=True)
    for unit in runtime_units():
        if unit == STRONGSWAN + '.service':
            # charon still holds the fixture's credentials and connections.
            systemctl('restart' if unit in saved['active'] else 'stop', unit).check_returncode()
        elif unit in saved['active']:
            systemctl('start', unit).check_returncode()
    print('services restored: ' + (' '.join(saved['active']) or 'none were active'), flush=True)


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
        process = subprocess.Popen(['openvpn', '--config', str(at('/tmp/vpn2-svc/client.conf'))],
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
            process = subprocess.Popen(['openvpn', '--config', str(at('/tmp/vpn2-svc/client.conf'))],
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
    # preserve() left empty directories and stopped the installation's units.
    if subprocess.run(['ip', 'link', 'show', 'wg0'], capture_output=True, timeout=5).returncode == 0:
        command('ip', 'link', 'delete', 'wg0')
    private = command('wg', 'genkey').strip()
    write('/etc/wireguard/wg0.conf', '[Interface]\nAddress = 10.66.66.1/24\n'
          'ListenPort = 51820\nPrivateKey = ' + private + '\n')
    command('ip', 'link', 'add', 'wg0', 'type', 'wireguard')
    command('ip', 'address', 'add', '10.66.66.1/24', 'dev', 'wg0')
    command('ip', 'link', 'set', 'wg0', 'up')
    base = at('/etc/openvpn/server/pki')
    (base / 'newcerts').mkdir(parents=True, exist_ok=True)
    at('/etc/openvpn/server/ccd').mkdir(exist_ok=True)
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
    write('/etc/swanctl/swanctl.conf', 'include conf.d/*.conf\n', 0o644)
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
    command('systemctl', 'restart', STRONGSWAN)
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
    preflight()
    saved = preserve()
    try:
        prepare()
        exercise()
    finally:
        restore(saved)
    print('live control API: PASS')


def exercise():
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
    base = at('/etc/openvpn/server/pki')
    verification = subprocess.run(['openssl', 'verify', '-crl_check', '-CAfile', str(base / 'ca.crt'),
                                  '-CRLfile', str(base / 'crl.pem'), str(base / 'client.crt')],
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
    if subprocess.run(['id', STRANGER], capture_output=True, timeout=5).returncode:
        command('useradd', '--system', '--groups', 'vpn2-control', STRANGER)
    print(command('runuser', '-u', STRANGER, '--', 'python3', __file__, '--group-only').strip())
    assert pwd.getpwnam('vpn2-control').pw_uid != 0


if __name__ == '__main__':
    main()
