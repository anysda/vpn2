"""Локальные регрессии DNS; реальные стадии деплоя здесь не запускаются."""

import json
from pathlib import Path
import unittest

from router_support import exit_cert, render


ROOT = Path(__file__).resolve().parents[2]
UPSTREAMS = ["[/ru/рф/xn--p1ai/su/]77.88.8.8",
             "https://1.1.1.1/dns-query", "https://dns.google/dns-query"]


def generate(mode="off"):
    env = dict(EXIT_TAGS="test", DOMAIN_TEST="exit.example.test",
               TEST_DIRECT="test-direct", TEST_WARP="test-warp", TEST_MGMT="test-mgmt",
               TEST_OBFS="test-obfs", MGMT_IP_TEST="192.0.2.2", MGMT_IP="192.0.2.1",
               HY2_DIRECT_PORT="443", HY2_WARP_PORT="8443", HY2_MGMT_PORT="9443",
               WG_OUT_IFACE="eth0", RU_CLASH_SECRET="test-secret", YT_ROUTE=mode,
               LANE_SUBNETS="198.51.100.0/24", TEST_HY2_CERT_PATH=exit_cert())
    return json.loads(render(env).stdout)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
