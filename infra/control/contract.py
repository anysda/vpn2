"""VPN control API v1, one UTF-8 JSON object + LF per Unix connection.

Request: {api: 1, method: <below>, params: <exact object>}. No extra fields.
Response: {api: 1, ok: true, result: object} or
{api: 1, ok: false, error: validation|forbidden|stale|conflict|unavailable|limit}.
Errors never contain submitted values, command output, or secrets.

wg.apply: {version: positive integer,
           peers: [{key: base64 public key, preshared_key: base64 key, ip: IPv4}]}
    IPv6 ULA is derived using the existing panel mapping. PSKs are input only.
openvpn.apply: {version, device_id: positive integer, action: allow|deny|revoke,
                ip: IPv4}. Certificates are located by dev_<device_id> in the
installed CA index, never by a caller-supplied path/serial/PEM. Revocation is
irreversible; allow cannot restore a revoked certificate. Issuance/export is
not part of v1; existing profiles remain valid.
ikev2.apply: {version, clients: [{device_id, username, password, ip}]}
    A full set of enabled devices. Removed/changed credentials have their
    concrete numeric IKE SA IDs terminated after loading the new credentials.
state.read: {protocol: wg|openvpn|ikev2}
config.apply: {resource: wg-peers|ikev2-clients, schema: 1, version,
               config: {peers: [...]}|{clients: [...]}}
    Same validation, version domain and application as the protocol method.
check.run: {check: readiness|wg|openvpn|ikev2}

Version domains: all WG peers; all IKEv2 clients; each OpenVPN device.
An accepted version/digest is persisted BEFORE effects. Older versions and
reuse with different data are rejected, including after a failed operation or
restart. Equal retries reapply and observe live state, never cached success.
One server process serializes all requests and holds an exclusive state lock.
Limits: request 1 MiB; response/command output 1 MiB; 2000 devices;
command 10 s; total request 30 s; socket read 3 s. Output is structured only.

Identity: filesystem socket 0660 root:vpn2-control; SO_PEERCRED UID must also
match the root-owned host-UID allowlist. Supplementary group membership alone
does not authorize a request. Container user namespaces must use the mapped
HOST UID in this file; the container UID is not trusted. Default: root (deploy
health probe) and the dedicated vpn2-panel UID. Panel migration is separate.

No private key, CA key, EAP password, or client profile is returned by v1.
The caller already holds the user EAP passwords and WG preshared keys submitted
to the corresponding apply methods.
Server keys/CA never enter JSON or diagnostic arguments.
"""

import base64
import ipaddress
import json
from pathlib import Path
import re

API = 1
LIMIT = 1024 * 1024
MAX_DEVICES = 2000
METHODS = ('wg.apply', 'openvpn.apply', 'ikev2.apply', 'state.read',
           'config.apply', 'check.run')


class Fault(Exception):
    pass


def require(condition, code='validation'):
    if not condition:
        raise Fault(code)


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()))


def integer(value):
    require(type(value) is int and 0 < value <= 2**53 - 1)


def choice(value, values):
    require(type(value) is str and value in values)


def address(value, protocol, pools):
    require(type(value) is str)
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise Fault('validation') from None
    networks = [ipaddress.ip_network(item) for item in pools[protocol]['subnets']]
    require(any(ip in net and ip not in (net.network_address, net.broadcast_address,
                                        net.network_address + 1) for net in networks))


def key(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9+/]{43}=', value))
    raw = base64.b64decode(value, validate=True)
    require(len(raw) == 32 and any(raw) and base64.b64encode(raw).decode() == value)


def records(value):
    require(type(value) is list and len(value) <= MAX_DEVICES)


def unique(items):
    require(len(items) == len(set(items)))


def validate(request, pools):
    fields(request, 'api method params')
    require(type(request['api']) is int and request['api'] == API)
    method, params = request['method'], request['params']
    choice(method, METHODS)
    if method == 'config.apply':
        fields(params, 'resource schema version config')
        require(type(params['schema']) is int and params['schema'] == 1)
        choice(params['resource'], ('wg-peers', 'ikev2-clients'))
        name = 'peers' if params['resource'] == 'wg-peers' else 'clients'
        fields(params['config'], name)
        method = 'wg.apply' if name == 'peers' else 'ikev2.apply'
        params = {'version': params['version'], name: params['config'][name]}
    if method == 'wg.apply':
        fields(params, 'version peers')
        records(params['peers'])
        for peer in params['peers']:
            fields(peer, 'key preshared_key ip')
            key(peer['key'])
            key(peer['preshared_key'])
            address(peer['ip'], 'wg', pools)
        unique([peer['key'] for peer in params['peers']])
        unique([peer['ip'] for peer in params['peers']])
    elif method == 'openvpn.apply':
        fields(params, 'version device_id action ip')
        integer(params['device_id'])
        choice(params['action'], ('allow', 'deny', 'revoke'))
        address(params['ip'], 'ovpn', pools)
    elif method == 'ikev2.apply':
        fields(params, 'version clients')
        records(params['clients'])
        for client in params['clients']:
            fields(client, 'device_id username password ip')
            integer(client['device_id'])
            require(type(client['username']) is str and
                    re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}', client['username']))
            require(type(client['password']) is str and
                    re.fullmatch(r'[A-Za-z0-9_@%+=:.,/-]{12,128}', client['password']))
            address(client['ip'], 'ikev2', pools)
        for name in ('device_id', 'username', 'ip'):
            unique([client[name] for client in params['clients']])
    elif method == 'state.read':
        fields(params, 'protocol')
        choice(params['protocol'], ('wg', 'openvpn', 'ikev2'))
    elif method == 'check.run':
        fields(params, 'check')
        choice(params['check'], ('readiness', 'wg', 'openvpn', 'ikev2'))
    if method.endswith('.apply'):
        integer(params['version'])
    return method, params


def load_pools():
    return json.loads(Path(__file__).with_name('vpn-pools.json').read_text())
