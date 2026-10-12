"""Real crypto, actual stage blocks; only inject failures at command boundaries.

VPN2_TEST_ROOT selects an older checkout for the before/after regression.
Requires strongswan-pki, openssl, wireguard-tools, openvpn and Python 3.
"""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get("VPN2_TEST_ROOT", Path(__file__).resolve().parents[2]))
WRAPPER = """#!/usr/bin/python3
import os
from pathlib import Path
import subprocess
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
phase = name + ':' + (args[0] if args else '')
if name == 'openssl' and args and args[0] == 'genpkey':
    phase += ':' + Path(args[args.index('-out') + 1]).name
if name == 'openssl' and args and args[0] in ('req', 'ca'):
    phase += ':' + ('-x509' if '-x509' in args else '-gencrl' if '-gencrl' in args else 'sign')
fail = phase == os.environ.get('PKI_FAIL')
mode = os.environ.get('PKI_FAIL_MODE', 'before')
if mode == 'once' and Path(os.environ['PKI_MARKER']).exists():
    fail = False
if fail:
    Path(os.environ['PKI_MARKER']).touch()
    if mode in ('before', 'once'):
        sys.exit(23)
    if mode == 'empty':
        sys.exit(0)
result = subprocess.run([os.environ['REAL_' + name.upper()], *args], check=False)
sys.exit(23 if fail else result.returncode)
"""


class PkiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vpn2-pki-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ)
        for name in ("openssl", "pki", "wg", "openvpn", "python3"):
            binary = shutil.which(name)
            self.assertIsNotNone(binary, f"install {name} to run PKI tests")
            self.env[f"REAL_{name.upper()}"] = binary
            wrapper = self.bin / name
            wrapper.write_text(WRAPPER)
            wrapper.chmod(0o755)
        self.env["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        self.env["PKI_MARKER"] = str(self.base / "fault-reached")

    def stage(self, protocol, fault="", mode="before"):
        names = {"ike": "27-ikev2", "wg": "28-wireguard", "ovpn": "29-openvpn"}
        body = (ROOT / "infra/scripts" / f"{names[protocol]}.sh").read_text()
        if protocol == "ike":
            body = body.split('  echo "[$HOST_TAG] [2/8] self-signed CA"', 1)[1]
            body = body.split('  install -m 644 "$PKI/ca.crt"', 1)[0]
        elif protocol == "wg":
            body = body.split('echo "[$HOST_TAG] [2/5] server keys"', 1)[1]
            body = body.split("# ULA fast-fail:", 1)[0]
        else:
            body = body.split("# ── 2. openssl.cnf", 1)[1].split("\n", 1)[1]
            body = body.split("# ── 4. server.conf", 1)[0]
        target = self.base / protocol
        target.mkdir(exist_ok=True)
        if protocol == "ovpn":
            (target / "newcerts").mkdir(exist_ok=True)
        body = body.replace("/etc/wireguard", str(target))
        body = body.replace("/etc/openvpn/server/pki", str(target))
        lib = ROOT / "infra/lib/anysda-pki.sh"
        prelude = f"source {shlex.quote(str(lib))}\n" if lib.exists() else ""
        command = "set -euo pipefail\n" + prelude
        command += f"HOST_TAG=ru\nENTRY_HOST=203.0.113.10\nPKI={shlex.quote(str(target))}\n"
        command += body
        env = dict(self.env, PKI_FAIL=fault, PKI_FAIL_MODE=mode)
        return subprocess.run(["bash", "-c", command], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)

    def good(self, protocol):
        result = self.stage(protocol)
        self.assertEqual(result.returncode, 0, result.stdout)
        target = self.base / protocol
        if protocol == "wg":
            pub = subprocess.check_output([self.env["REAL_WG"], "pubkey"],
                                          input=(target / "server.priv").read_bytes())
            self.assertEqual(pub, (target / "server.pub").read_bytes())
            return
        for stem in ("ca", "server"):
            key = subprocess.check_output(["openssl", "pkey", "-in", str(target / f"{stem}.key"), "-pubout"])
            cert = subprocess.check_output(["openssl", "x509", "-in", str(target / f"{stem}.crt"), "-pubkey", "-noout"])
            self.assertEqual(key, cert)
        subprocess.run(["openssl", "verify", "-CAfile", str(target / "ca.crt"),
                        "-purpose", "sslserver", str(target / "server.crt")], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def snapshot(self, protocol):
        return {str(p.relative_to(self.base / protocol)): p.read_bytes()
                for p in (self.base / protocol).rglob("*") if p.is_file()}

    def test_failed_generation_retry(self):
        phases = {
            "ike": ("openssl:genpkey:ca.key", "openssl:genpkey:server.key",
                    "pki:--self", "pki:--pub", "pki:--issue", "python3:-"),
            "wg": ("wg:genkey", "wg:pubkey", "python3:-"),
            "ovpn": ("openssl:genpkey:ca.key", "openssl:genpkey:server.key",
                     "openssl:req:-x509", "openssl:req:sign",
                     "openssl:ca:sign", "openvpn:--genkey", "openssl:ca:-gencrl", "python3:-"),
        }
        for protocol, faults in phases.items():
            for fault in faults:
                for mode in ("before", "after", "empty"):
                    # После успешного rename уже опубликован полный комплект;
                    # отказ проверяем до syscall (без его подмены успехом).
                    if fault == "python3:-" and mode != "before":
                        continue
                    with self.subTest(protocol=protocol, fault=fault, mode=mode):
                        shutil.rmtree(self.base / protocol, ignore_errors=True)
                        Path(self.env["PKI_MARKER"]).unlink(missing_ok=True)
                        result = self.stage(protocol, fault, mode)
                        self.assertTrue(Path(self.env["PKI_MARKER"]).exists(), result.stdout)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        # Никакие части нового комплекта не видны после сбоя.
                        self.assertFalse(any(p.suffix in (".key", ".crt", ".priv", ".pub")
                                             for p in (self.base / protocol).rglob("*")))
                        self.good(protocol)
                        before = self.snapshot(protocol)
                        self.good(protocol)
                        self.assertEqual(before, self.snapshot(protocol))

    def test_private_keys_are_not_world_readable(self):
        for protocol in ("ike", "wg", "ovpn"):
            with self.subTest(protocol=protocol):
                self.good(protocol)
                for key in (self.base / protocol).glob("*"):
                    if key.suffix in (".key", ".priv"):
                        self.assertEqual(key.stat().st_mode & 0o777, 0o600, key.name)

    def test_wireguard_transient_pubkey_failure_never_rotates_private(self):
        self.good("wg")
        target = self.base / "wg"
        private = (target / "server.priv").read_bytes()
        for name in ("server.priv", "server.pub"):
            (target / name).unlink()
        shutil.rmtree(target / "keys", ignore_errors=True)
        (target / "server.priv").write_bytes(private)
        before = self.snapshot("wg")
        result = self.stage("wg", "wg:pubkey", "once")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(before, self.snapshot("wg"))
        self.good("wg")
        self.assertEqual(private, (target / "server.priv").read_bytes())

    def test_legacy_incomplete_bundle_keeps_keys(self):
        for protocol in ("ike", "wg", "ovpn"):
            with self.subTest(protocol=protocol):
                self.good(protocol)
                target = self.base / protocol
                if protocol == "wg":
                    # Старый плоский формат без каталога keys и ссылок.
                    private = (target / "server.priv").read_bytes()
                    for name in ("server.priv", "server.pub"):
                        (target / name).unlink()
                    shutil.rmtree(target / "keys", ignore_errors=True)
                    (target / "server.priv").write_bytes(private)
                    broken, fault = "server.pub", "wg:pubkey"
                else:
                    broken = "ca.crt" if protocol == "ike" else "server.crt"
                    fault = "pki:--self" if protocol == "ike" else "openssl:ca:sign"
                (target / broken).write_bytes(b"")
                before = self.snapshot(protocol)
                failed = self.stage(protocol, fault, "after")
                self.assertNotEqual(failed.returncode, 0, failed.stdout)
                self.assertEqual(before, self.snapshot(protocol))
                self.good(protocol)
                after = self.snapshot(protocol)
                for name, content in before.items():
                    if name.endswith((".key", ".priv")):
                        self.assertEqual(content, after[name], name)

    def test_each_missing_or_empty_public_part(self):
        for protocol in ("ike", "wg", "ovpn"):
            self.good(protocol)
            target = self.base / protocol
            names = ("server.pub",) if protocol == "wg" else ("ca.crt", "server.crt")
            if protocol == "ovpn":
                names += ("tls-crypt.key", "crl.pem")
            for name in names:
                for empty in (False, True):
                    with self.subTest(protocol=protocol, name=name, empty=empty):
                        keys = {p.name: p.read_bytes() for p in target.glob("*")
                                if p.name in ("ca.key", "server.key", "server.priv")}
                        (target / name).unlink()
                        if empty:
                            (target / name).write_bytes(b"")
                        self.good(protocol)
                        for key, content in keys.items():
                            self.assertEqual(content, (target / key).read_bytes())

    def test_empty_private_key_from_interrupted_old_stage(self):
        for protocol in ("ike", "wg", "ovpn"):
            with self.subTest(protocol=protocol):
                target = self.base / protocol
                target.mkdir()
                key = "server.priv" if protocol == "wg" else "ca.key"
                (target / key).touch()
                self.good(protocol)

    def test_valid_certificate_without_key_is_not_rotated(self):
        for protocol in ("ike", "ovpn"):
            self.good(protocol)
            target = self.base / protocol
            for key in ("ca.key", "server.key"):
                with self.subTest(protocol=protocol, key=key):
                    content = (target / key).read_bytes()
                    (target / key).write_bytes(b"")
                    before = self.snapshot(protocol)
                    result = self.stage(protocol)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(before, self.snapshot(protocol))
                    (target / key).write_bytes(content)

    def test_openvpn_issued_and_revoked_clients_survive(self):
        self.good("ovpn")
        target = self.base / "ovpn"
        key, csr, cert = (str(self.base / name) for name in ("client.key", "client.csr", "client.crt"))
        commands = [
            ["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=regression-client"],
            ["openssl", "ca", "-batch", "-config", str(target / "openssl.cnf"), "-extensions", "client_ext",
             "-in", csr, "-out", cert],
            ["openssl", "ca", "-config", str(target / "openssl.cnf"), "-revoke", cert],
            ["openssl", "ca", "-config", str(target / "openssl.cnf"), "-gencrl", "-out", str(target / "crl.pem")],
        ]
        for command in commands:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        before = self.snapshot("ovpn")
        (target / "server.crt").write_bytes(b"")
        self.good("ovpn")
        for name in ("ca.key", "ca.crt", "server.key", "tls-crypt.key", "crl.pem"):
            self.assertEqual(before[name], (target / name).read_bytes())
        self.assertIn(b"R\t", (target / "index.txt").read_bytes())
        result = subprocess.run(["openssl", "verify", "-CAfile", str(target / "ca.crt"),
                                 "-CRLfile", str(target / "crl.pem"), "-crl_check", cert],
                                text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate revoked", result.stderr)


if __name__ == "__main__":
    unittest.main()
