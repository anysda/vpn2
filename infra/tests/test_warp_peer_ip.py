"""Адрес WARP-пира в конфиге sing-box экзита - IP-литерал (VPN2-650).

Прод: sing-box экзита разрешает домен пира при старте. Экзит перезагрузился
раньше DNS или engage.cloudflareclient.com не разрешается - FATAL «resolve
endpoint domain for peer[0]», и вместе с WARP-ногой падают direct-ноги.
Деплой должен класть в конфиг IP: домен разрешает сам, не разрешился -
запасной anycast WARP 162.159.192.1 со строкой в выводе.
"""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
DOMAIN = 'engage.cloudflareclient.com'
RESOLVED = '198.51.100.7'
FALLBACK = '162.159.192.1'

WGCF = '''#!/bin/sh
case "$1" in
  generate)
    test "${WGCF_GENERATE_FAIL:-0}" = 0 || { echo 'i/o timeout' >&2; exit 1; }
    printf '[Interface]\\nPrivateKey = live-key\\nAddress = 172.16.0.2/32, fd00::2/128\\n' > wgcf-profile.conf
    printf '[Peer]\\nPublicKey = peer-key\\nEndpoint = %s\\n' "$WGCF_ENDPOINT" >> wgcf-profile.conf
    ;;
  *) exit 1 ;;
esac
'''

# Подменные резолверы: отвечают RESOLVE_IP, пустой - «не разрешается».
RESOLVERS = {
    'getent': '#!/bin/sh\n[ -n "$RESOLVE_IP" ] || exit 2\necho "$RESOLVE_IP STREAM $3"\n',
    'dig': '#!/bin/sh\n[ -z "$RESOLVE_IP" ] || echo "$RESOLVE_IP"\n',
    'host': '#!/bin/sh\n[ -n "$RESOLVE_IP" ] || { echo "Host $1 not found"; exit 1; }\n'
            'echo "$1 has address $RESOLVE_IP"\n',
    'nslookup': '#!/bin/sh\n[ -n "$RESOLVE_IP" ] || { echo "** server can\'t find $1"; exit 1; }\n'
                'printf "Name:\\t%s\\nAddress: %s\\n" "$1" "$RESOLVE_IP"\n',
}
SITECUSTOMIZE = '''import os, socket
_ip = os.environ.get('RESOLVE_IP', '')
def _fake(host, port, *args, **kwargs):
    try:
        socket.inet_pton(socket.AF_INET, host)
        ip = host
    except (OSError, TypeError):
        if not _ip:
            raise socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        ip = _ip
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, int(port or 0)))]
socket.getaddrinfo = _fake
socket.gethostbyname = lambda host: _fake(host, 0)[0][4][0]
'''


class WarpPeerAddressTests(unittest.TestCase):
    """Кусок 10-foreign.sh от профиля WARP до рендера конфига, без сети."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'etc/anysda'
        self.singbox = self.root / 'etc/sing-box'
        self.stage = self.root / 'stage'
        self.bin = self.root / 'bin'
        self.py = self.root / 'py'
        for d in (self.state, self.singbox, self.stage, self.bin, self.py):
            d.mkdir(parents=True)
        (self.state / 'wgcf-account.toml').write_text(
            'access_token = "token"\ndevice_id = "device"\nkey_type = "curve25519"\n')
        shutil.copy(ROOT / 'infra/configs/sing-box-server.json.tpl', self.stage)
        shutil.copy(ROOT / 'infra/lib/hy2_bandwidth.py', self.stage)
        (self.stage / 'install-sing-box.sh').write_text('exit 0\n')
        fakes = dict(RESOLVERS, wgcf=WGCF,
                     curl='#!/bin/sh\ncat >/dev/null\necho \'{"config": {"client_id": "AQID"}}\'\n',
                     **{'sing-box': '#!/bin/sh\necho "sing-box version 1.12.25"\n'})
        for name, body in fakes.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.py / 'sitecustomize.py').write_text(SITECUSTOMIZE)

    def deploy(self, endpoint, resolve_ip=RESOLVED, generate_fails=False):
        source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
        start = source.index('cd /etc/anysda\n')
        end = source.index('\n# Validate', start)
        block = source[start:end]
        for real, fake in (('/usr/local/bin/', f'{self.bin}/'), ('/etc/anysda', str(self.state)),
                           ('/etc/sing-box', str(self.singbox)), ('/tmp/anysda', str(self.stage))):
            block = block.replace(real, fake)
        out = self.root / 'config.json'
        out.unlink(missing_ok=True)
        script = ('HOST_TAG=test PUB_IP=192.0.2.1 HY2_DIRECT_PORT=1443 HY2_WARP_PORT=2443 '
                  'HY2_MGMT_PORT=3443\n' + block + f'\ncp "$SB_CONFIG_TMP" {out}\n')
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', PYTHONPATH=str(self.py),
                   WGCF_ENDPOINT=endpoint, RESOLVE_IP=resolve_ip,
                   WGCF_GENERATE_FAIL=str(int(generate_fails)))
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.state,
                                text=True, capture_output=True, env=env)
        text = out.read_text() if out.exists() else ''
        return result, text, json.loads(text) if text else None

    def peer(self, config):
        warp = next(e for e in config['endpoints'] if e['tag'] == 'warp')
        return warp['peers'][0]

    def test_resolvable_domain_becomes_ip(self):
        result, text, config = self.deploy(f'{DOMAIN}:2408')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        peer = self.peer(config)
        self.assertEqual((peer['address'], peer['port']), (RESOLVED, 2408),
                         'в конфиг ушёл домен пира: без DNS sing-box падает FATAL целиком')
        self.assertNotIn(DOMAIN, text)

    def test_unresolvable_domain_falls_back_to_anycast(self):
        result, text, config = self.deploy(f'{DOMAIN}:2408', resolve_ip='')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        peer = self.peer(config)
        self.assertEqual((peer['address'], peer['port']), (FALLBACK, 2408),
                         'домен не разрешился, а в конфиге не запасной anycast WARP')
        self.assertNotIn(DOMAIN, text)
        self.assertIn(FALLBACK, result.stdout, 'нет строки в выводе деплоя про запасной адрес')
        self.assertIn({'type': 'direct', 'tag': 'direct'}, config['outbounds'])
        tags = {i['tag'] for i in config['inbounds']}
        self.assertTrue({'hy2-direct-in', 'hy2-warp-in'} <= tags, tags)

    def test_ip_literal_endpoint_kept(self):
        for endpoint, address in (('162.159.193.10:2408', '162.159.193.10'),
                                  ('[2606:4700:d0::a29f:c001]:2408', '2606:4700:d0::a29f:c001')):
            with self.subTest(endpoint=endpoint):
                result, _, config = self.deploy(endpoint, resolve_ip='')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                peer = self.peer(config)
                self.assertEqual((peer['address'], peer['port']), (address, 2408))

    def test_redeploy_gives_same_config(self):
        first = self.deploy(f'{DOMAIN}:2408')
        second = self.deploy(f'{DOMAIN}:2408')
        for result, _, _ in (first, second):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(first[1], second[1])

    def test_missing_profile_never_renders_empty_or_domain_peer(self):
        result, _, config = self.deploy(f'{DOMAIN}:2408', generate_fails=True)
        self.assertFalse((self.state / 'wgcf-profile.conf').exists())
        if result.returncode != 0 or config is None:
            return
        address = self.peer(config)['address']
        ipaddress.ip_address(address)  # пустой или домен - ValueError
        self.assertRegex(result.stdout, 'профил', 'WARP без профиля собран молча')


if __name__ == '__main__':
    unittest.main()
