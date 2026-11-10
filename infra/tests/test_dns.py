"""Локальные регрессии DNS; реальные стадии деплоя здесь не запускаются."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from adguard_support import Hub
from router_support import exit_cert
from vpn2.adguard import dns


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = Path(os.environ.get("GEN_ROUTER_PATH", ROOT / "infra/vpn2/src/vpn2/router/config.py"))
UPSTREAMS = ["[/ru/рф/xn--p1ai/su/]77.88.8.8",
             "https://1.1.1.1/dns-query", "https://dns.google/dns-query"]


def generate(mode="off"):
    env = dict(os.environ, EXIT_TAGS="test", DOMAIN_TEST="exit.example.test",
               TEST_DIRECT="test-direct", TEST_WARP="test-warp", TEST_MGMT="test-mgmt",
               TEST_OBFS="test-obfs", MGMT_IP_TEST="192.0.2.2", MGMT_IP="192.0.2.1",
               HY2_DIRECT_PORT="443", HY2_WARP_PORT="8443", HY2_MGMT_PORT="9443",
               WG_OUT_IFACE="eth0", RU_CLASH_SECRET="test-secret", YT_ROUTE=mode,
               LANE_SUBNETS="198.51.100.0/24", TEST_HY2_CERT_PATH=exit_cert())
    return json.loads(subprocess.check_output([sys.executable, str(GENERATOR)], env=env))


class RouterDnsTests(unittest.TestCase):
    def test_doh_foreign_before_manual_routes(self):
        for mode in ("off", "direct", "zapret"):
            with self.subTest(mode=mode):
                cfg = generate(mode)
                rules = cfg["route"]["rules"]
                doh = next((r for r in rules if r.get("outbound") == "foreign-best"
                            and r.get("port") == [443]), None)
                self.assertIsNotNone(doh, "DoH upstream rule -> foreign-best missing")
                self.assertEqual(set(doh["ip_cidr"]), {
                    "1.1.1.1/32", "8.8.8.8/32", "8.8.4.4/32",
                    "2001:4860:4860::8888/128", "2001:4860:4860::8844/128"})
                # Та же граница служебной головы, что в anysda-apply-routes.
                head = 0
                while head < len(rules) and ("inbound" in rules[head]
                                            or rules[head].get("action") == "reject"):
                    head += 1
                self.assertLess(rules.index(doh), head)
                inbounds = {i["tag"]: i for i in cfg["inbounds"]}
                for tag in doh["inbound"]:
                    self.assertEqual(inbounds[tag]["type"], "redirect")
                    self.assertEqual(inbounds[tag]["listen_port"], 7899)

    def test_dns_uses_unfiltered_hub_listener(self):
        cfg = generate()
        rules = cfg["route"]["rules"]
        dns = [r for r in rules if r.get("protocol") == "dns"]
        self.assertEqual(dns, [{"inbound": ["wg-tproxy-in"], "protocol": "dns",
                                "action": "route", "outbound": "dns-unfiltered",
                                "override_address": "127.0.0.2", "override_port": 53}])
        outbound = next(o for o in cfg["outbounds"] if o["tag"] == "dns-unfiltered")
        self.assertEqual(outbound, {"type": "direct", "tag": "dns-unfiltered",
                                    "bind_interface": "lo"})

    def test_bootstrap_has_no_adguard_cycle(self):
        cfg = generate()
        # Адреса экзитов (DOMAIN_<TAG>) резолвит только bootstrap-dns.
        self.assertEqual(cfg["route"]["default_domain_resolver"], {"server": "bootstrap-dns"})
        bootstrap = next(s for s in cfg["dns"]["servers"] if s["tag"] == "bootstrap-dns")
        self.assertEqual(bootstrap, {"type": "udp", "tag": "bootstrap-dns",
                                     "server": "8.8.8.8", "detour": "direct-ru"})
        self.assertEqual(cfg["dns"]["rules"], [{"domain_suffix": [".ru", ".рф", ".xn--p1ai", ".su"],
                                               "server": "ru-dns"}])


class AdguardPatchTests(unittest.TestCase):
    def test_installed_config_and_idempotence(self):
        before = yaml.safe_load(dns.fresh_text("test-user", "test-hash", "3000", "192.0.2.1", "true", "1000"))
        # Состояние установленного хаба, включая пользовательские настройки.
        before["dns"].update(upstream_dns=["77.88.8.8", *UPSTREAMS[1:],
                                           "[/youtube.com/googlevideo.com/]77.88.8.8"],
                              fallback_dns=["77.88.8.8"], cache_optimistic=False,
                              upstream_dns_file="/etc/custom-upstreams.txt", use_http3_upstreams=True)
        before["user_rules"] = ["||ads.example.test^"]
        before["clients"] = {"persistent": [{"name": "custom", "ids": ["192.0.2.10"]}]}
        with tempfile.TemporaryDirectory() as work:
            hub = Hub(work, user="test-user")
            hub.env.update(WG_SERVER_IP="192.0.2.1", OVPN_SUBNET="198.51.100.0", IKEV2_SUBNET="203.0.113.0/24")
            hub.config.write_text(yaml.safe_dump(before, allow_unicode=True, sort_keys=False))
            hub.deploy()
            self.assertTrue(hub.changed)
            after = yaml.safe_load(hub.config.read_text())
            self.assertEqual(after["dns"]["upstream_dns"], UPSTREAMS)
            self.assertEqual(after["dns"]["fallback_dns"], [])
            self.assertEqual(after["dns"]["upstream_dns_file"], "")
            self.assertTrue(after["dns"]["cache_optimistic"])
            self.assertFalse(after["dns"]["use_http3_upstreams"])
            self.assertEqual(after["users"], [{"name": "test-user", "password": "hash"}])
            for key in ("filters", "user_rules", "clients"):
                self.assertEqual(after[key], before[key])
            first = hashlib.sha256(hub.config.read_bytes()).hexdigest()
            hub.deploy()
            self.assertFalse(hub.changed)
            self.assertEqual(first, hashlib.sha256(hub.config.read_bytes()).hexdigest())

    def test_unfiltered_instance_has_same_policy_without_lists(self):
        filtered = {"users": [{"name": "test", "password": "hash"}],
                    "dns": {"upstream_dns": UPSTREAMS, "cache_optimistic": True},
                    "filters": [{"enabled": True}], "user_rules": ["||example.test^"]}
        plain = dns.plain_config(filtered, {}, "3000")
        self.assertEqual(plain["dns"]["upstream_dns"], UPSTREAMS)
        self.assertEqual(plain["dns"]["bind_hosts"], ["127.0.0.2"])
        self.assertFalse(plain["filtering"]["protection_enabled"])
        self.assertFalse(plain["filtering"]["filtering_enabled"])
        self.assertEqual(plain["filters"], [])
        self.assertEqual(plain["user_rules"], [])
        enriched = copy.deepcopy(plain)
        enriched["dns"]["unrelated_default"] = True
        enriched["schema_version"] = 30
        self.assertEqual(dns.plain_config(filtered, enriched, "3000"), enriched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
