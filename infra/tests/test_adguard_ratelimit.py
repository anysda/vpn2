"""DNS exemptions, including the pinned AdGuard binary when ADGUARD_BIN is set.

The live test needs iproute2 and CAP_NET_ADMIN in an isolated network namespace.
It uses the speedtest packet generator, real pool source addresses and local
rewrites, so upstream availability cannot hide a rate-limit regression.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

from test_deploy import ROOT, bash, source


def patch_config(path):
    stage = source('infra/scripts/22-adguard.sh')
    function = re.search(r'^patch_agh_config\(\) \{\n.*?^\}', stage, re.M | re.S).group()
    result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\n'
                  'HOST_TAG=test\nMGMT_IP=192.0.2.53\nsvc_forget() { :; }\n'
                  + function + f'\npatch_agh_config "{path}"', path.parent)
    if result.returncode:
        raise AssertionError(result.stderr)
    return yaml.safe_load(path.read_text())


def pool_clients():
    pools = json.loads(source('app/shared/vpn-pools.json'))
    return {key: [str(ipaddress.ip_network(cidr).network_address + 2)
                  for cidr in pools[key]['subnets']] for key in ('wg', 'ovpn', 'ikev2')}


class AdguardRatelimitTests(unittest.TestCase):
    def test_tunnel_sources_are_exempt_on_fresh_and_existing_hubs(self):
        pools = json.loads(source('app/shared/vpn-pools.json'))
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'AdGuardHome.yaml'
                dns = {'aaaa_disabled': True}
                if existing:
                    dns.update(ratelimit=20, ratelimit_subnet_len_ipv4=24,
                               ratelimit_whitelist=['203.0.113.8'], bind_hosts=['0.0.0.0'])
                path.write_text(yaml.safe_dump({'dns': dns}))
                config = patch_config(path)['dns']
                addresses = set(config['ratelimit_whitelist'])
                for protocol in ('wg', 'ovpn', 'ikev2'):
                    for cidr in pools[protocol]['subnets']:
                        for address in ipaddress.ip_network(cidr).hosts():
                            self.assertIn(str(address), addresses, (protocol, cidr, address))
                if existing:
                    self.assertIn('203.0.113.8', addresses)
                # v0.107.79 parses the whitelist but does not pass it to dnsproxy.
                # Membership alone does not mean that the client is exempt.
                self.assertEqual(config.get('ratelimit', 20), 0)
                self.assertEqual(config['bind_hosts'], ['127.0.0.1', '192.0.2.53'])
                before = path.read_bytes()
                patch_config(path)
                self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.environ.get('ADGUARD_BIN'), 'set ADGUARD_BIN in an isolated network namespace')
    def test_live_single_and_parallel_pool_clients(self):
        spec = importlib.util.spec_from_file_location('speedtest', ROOT / 'infra/tools/speedtest.py')
        speedtest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(speedtest)
        binary = os.environ['ADGUARD_BIN']
        version = re.search(r"^AGH_VER='([^']+)'", source('infra/scripts/22-adguard.sh'), re.M)[1]
        self.assertIn(version, subprocess.check_output([binary, '--version'], text=True))
        clients = pool_clients()
        addresses = ['192.0.2.53', *(a for group in clients.values() for a in group)]
        added = []
        try:
            for address in addresses:
                subprocess.run(['ip', 'addr', 'add', address + '/32', 'dev', 'lo'], check=True)
                added.append(address)
            with tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                path = work / 'AdGuardHome.yaml'
                path.write_text(yaml.safe_dump({'dns': {'aaaa_disabled': True, 'ratelimit': 20,
                                                       'bind_hosts': ['127.0.0.1', '192.0.2.53']}}))
                config = patch_config(path)
                # Local answers isolate request admission from upstream routing.
                config.update(http={'address': '127.0.0.1:3000'}, users=[], filters=[],
                              schema_version=29, log={'verbose': True},
                              filtering={'rewrites': [{'domain': name, 'answer': '192.0.2.80', 'enabled': True}
                                                       for name in speedtest.DNS_DOMAINS]})
                config['dns'].update(port=53, upstream_dns=['127.0.0.1:9'],
                                     bootstrap_dns=[], fallback_dns=[], use_private_ptr_resolvers=False)
                path.write_text(yaml.safe_dump(config))
                print(f"AdGuard {version}: ratelimit={config['dns'].get('ratelimit', 20)} "
                      f"whitelist_len={len(config['dns']['ratelimit_whitelist'])}", flush=True)
                with (work / 'adguard.log').open('w+') as log:
                    process = subprocess.Popen([binary, '-c', str(path), '-w', str(work), '--no-check-update'],
                                               stdout=log, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 15
                        while True:
                            try:
                                with socket.create_connection(('192.0.2.53', 53), timeout=0.1):
                                    break
                            except OSError:
                                if process.poll() is not None or time.monotonic() > deadline:
                                    log.seek(0)
                                    self.fail(log.read())
                                time.sleep(0.05)

                        def burst(label, address, start):
                            probe = speedtest.DNS_PROBE.replace('s.setblocking(False)',
                                                               f's.bind(({address!r}, 0))\ns.setblocking(False)')
                            # Count successful A answers, never SERVFAIL/REFUSED.
                            probe = probe.replace('if qid in sent and qid not in lat:',
                                                  'if qid in sent and qid not in lat and '
                                                  'data[3] & 15 == 0 and '
                                                  'data[-4:] == socket.inet_aton("192.0.2.80"):')
                            result = subprocess.check_output([sys.executable, '-c', probe, '192.0.2.53',
                                                              '100', '1', '2', str(start),
                                                              ','.join(speedtest.DNS_DOMAINS)], text=True, timeout=10)
                            count = len(json.loads(result)['lat_ms'])
                            print(f'{label}: {count}/100', flush=True)
                            return count

                        counts = [burst('one wg', clients['wg'][0], time.time() + 0.2)]
                        # Leave no tokens from the single-client control in the shared bucket.
                        time.sleep(1.1)
                        for index in range(len(clients['wg'])):
                            start = time.time() + 0.3
                            with ThreadPoolExecutor(max_workers=3) as executor:
                                futures = [executor.submit(burst, f'all {key} pool={index}', group[index], start)
                                           for key, group in clients.items()]
                                counts.extend(f.result() for f in futures)
                        self.assertEqual(counts, [100] * len(counts))
                    finally:
                        process.terminate()
                        process.wait(timeout=10)
        finally:
            for address in reversed(added):
                subprocess.run(['ip', 'addr', 'del', address + '/32', 'dev', 'lo'], check=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
