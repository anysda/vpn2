"""Ресурсные профили и команды стадий без Docker/root.

ENTRY_MEMORY_ROOT — дерево для регрессии на origin/dev или 42359a1.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(os.environ.get("ENTRY_MEMORY_ROOT", Path(__file__).resolve().parents[2]))
TOOL = ROOT / "infra/lib/entry_resources.py"
SERVICES = {"panel": "30-frontend.sh", "bot": "35-telegram.sh", "vm": "25-monitoring.sh"}


def stage(name):
    return (ROOT / "infra/scripts" / name).read_text()


def module():
    spec = importlib.util.spec_from_file_location("entry_resources", TOOL)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def render(ram, disk, service, overrides=None):
    return subprocess.run(
        [sys.executable, str(TOOL), "--ram-mib", str(ram), "--free-mib", str(disk),
         "--service", service], check=True, capture_output=True, text=True,
        env=dict(os.environ, ENTRY_RESOURCE_OVERRIDES=json.dumps(overrides or {}))).stdout


def docker_args(service, ram, disk=8192, overrides=None):
    command = re.search(r"^docker run -d .*?(?=\n\n)", stage(SERVICES[service]), re.M | re.S)[0]
    # Старые деревья тоже исполняются: проверяем реальные неверные флаги,
    # а не только отсутствие нового модуля.
    setup = render(ram, disk, service, overrides) if TOOL.exists() else ""
    result = subprocess.run(
        ["bash", "-c", setup + "\ncat() { :; }; docker() { printf '%s\\0' \"$@\" >&2; }; " + command],
        capture_output=True, check=True,
    )
    return result.stderr.decode().split("\0")[:-1]


class EntryMemoryTest(unittest.TestCase):
    def test_large_machine_preserves_origin_dev(self):
        for ram in (4096, 8192):
            for service in SERVICES:
                with self.subTest(ram=ram, service=service):
                    args = docker_args(service, ram)
                    self.assertNotIn("--memory", args)
                    self.assertFalse(any(arg.startswith("NODE_OPTIONS=") for arg in args))
                    if service == "vm":
                        self.assertIn("-retentionPeriod=7d", args)
                        self.assertFalse(any(arg.startswith(("-memory.", "-search.max")) for arg in args))

    def test_two_gib_light_runtime(self):
        for service, budget in (("panel", "384m"), ("bot", "128m"), ("vm", "256m")):
            args = docker_args(service, 2048)
            self.assertIn("--memory", args)
            self.assertEqual(args[args.index("--memory") + 1], budget)
            self.assertEqual(args[args.index("--memory-swap") + 1], budget)
        self.assertIn("NODE_OPTIONS=--max-old-space-size=256", docker_args("panel", 2048))
        args = docker_args("vm", 2048)
        for flag in ("-retentionPeriod=3d", "-memory.allowedBytes=64MiB",
                     "-search.maxMemoryPerQuery=16MiB", "-search.maxConcurrentRequests=4",
                     "-search.latencyOffset=1s"):
            self.assertIn(flag, args)
        self.assertIn("scrape_interval: 2s", stage("25-monitoring.sh"))

    def test_expected_table(self):
        calc = module().calculate
        keys = ("panel_memory_mib", "panel_old_space_mib", "bot_memory_mib", "vm_memory_mib",
                "vm_cache_mib", "vm_query_mib", "vm_concurrent_requests", "agh_memory_mib",
                "agh_high_mib", "agh_go_mib", "retention_days", "adaway_enabled", "querylog_size")
        rows = [
            (1024, (192, 128, 64, 128, 32, 8, 2, 128, 96, 80, 1, False, 50)),
            (2048, (384, 256, 128, 256, 64, 16, 4, 256, 192, 160, 3, False, 100)),
            (3072, (768, 512, 256, 512, 128, 32, 8, 512, 384, 320, 5, True, 500)),
            (4096, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 7, True, 1000)),
            (8192, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 7, True, 1000)),
        ]
        for ram, expected in rows:
            for free, disk_days in ((0, 1), (1024, 1), (2048, 1), (4096, 3), (8192, 7), (65536, 63)):
                with self.subTest(ram=ram, disk=free):
                    row = dict(zip(keys, expected))
                    row["retention_days"] = min(row["retention_days"], disk_days)
                    row["adaway_enabled"] = ram >= 3072 and free >= 2048
                    self.assertEqual(calc(ram, free, {}), row)
                    self.assertEqual(calc(ram, free, {}), calc(ram, free, {}))

    def test_kernel_reserve_and_boundaries(self):
        calc = module().calculate
        for boundary, nominal in ((1920, 2048), (2880, 3072), (3840, 4096)):
            self.assertEqual(calc(boundary, 8192, {}), calc(nominal, 8192, {}))
            self.assertNotEqual(calc(boundary - 1, 8192, {}), calc(boundary, 8192, {}))

    def test_hardware_reads_memtotal_and_data_filesystem(self):
        resource = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meminfo = root / "meminfo"
            meminfo.write_text("MemTotal: 2097152 kB\nMemAvailable: 123 kB\n")
            disk = os.statvfs_result((4096, 4096, 10000000, 1000000, 524288, 0, 0, 0, 0, 255))
            with patch.object(resource.os, "statvfs", return_value=disk) as probe:
                self.assertEqual(resource.hardware(root / "missing/data", meminfo), (2048, 2048))
                probe.assert_called_once_with(root)

    def test_explicit_values_including_zero_and_false(self):
        calc = module().calculate
        overrides = dict.fromkeys(calc(2048, 8192, {}), "0")
        overrides.update(retention_days="12", adaway_enabled="false")
        values = calc(8192, 1024, overrides)
        self.assertEqual(values["retention_days"], 12)
        self.assertFalse(values["adaway_enabled"])
        self.assertEqual(values["querylog_size"], 0)
        self.assertNotIn("--memory", docker_args("panel", 1024, overrides=overrides))
        overrides.update(panel_memory_mib="640", panel_old_space_mib="384")
        args = docker_args("panel", 8192, overrides=overrides)
        self.assertEqual(args[args.index("--memory") + 1], "640m")
        self.assertIn("NODE_OPTIONS=--max-old-space-size=384", args)
        for bad in ({"typo": 1}, {"retention_days": 0}, {"vm_cache_mib": -1},
                    {"adaway_enabled": "maybe"}, {"querylog_size": "$(id)"}):
            with self.assertRaises(ValueError):
                calc(2048, 8192, bad)

    def test_config_yaml_to_stage_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                (ROOT / "config.example.yaml").read_text()
                .replace('  passphrase: ""', '  passphrase: test pass')
                + "\nentry_resources:\n  querylog_size: 0\n  adaway_enabled: false\n  retention_days: 11\n")
            subprocess.run([sys.executable, str(ROOT / "infra/lib/config2env.py"), str(root)],
                           capture_output=True, check=True)
            command = (f"source {shlex.quote(str(root / 'infra/envs/ru.env'))}; "
                       f"source {shlex.quote(str(ROOT / 'infra/lib/entry-resources.sh'))}; "
                       "entry_resources vm; printf '%s\\n' \"${ENTRY_RUNTIME_ARGS[@]}\"")
            result = subprocess.run(["bash", "-eu", "-c", command], capture_output=True, check=True, text=True)
            self.assertIn("-retentionPeriod=11d", result.stdout)

    def test_stages_load_and_deploy_ships_library(self):
        for service, name in {**SERVICES, "agh": "22-adguard.sh"}.items():
            script = stage(name)
            self.assertIn(f"entry_resources {service}", script)
            self.assertIn('source "$RESOURCE_LIB"', script)
        deploy = (ROOT / "infra/deploy.sh").read_text()
        for filename in ("entry-resources.sh", "entry_resources.py"):
            self.assertIn(f'push "$DEPLOY_ROOT/lib/{filename}"', deploy)

    def test_deployed_library_and_invalid_override_fail_fast(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in ("entry-resources.sh", "entry_resources.py"):
                shutil.copy(ROOT / "infra/lib" / filename, root)
            command = f"source {shlex.quote(str(root / 'entry-resources.sh'))}; entry_resources vm"
            result = subprocess.run(["bash", "-eu", "-c", command], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(["bash", "-eu", "-c", command + "; echo SHOULD_NOT_RUN"],
                                    env=dict(os.environ, ENTRY_RESOURCE_OVERRIDES='{"retention_days":0}'),
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"SHOULD_NOT_RUN", result.stdout)

    def test_cli_recomputes_after_hardware_change(self):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            command = [sys.executable, str(TOOL), "--meminfo", str(meminfo), "--free-mib", "8192"]
            for ram, expected in ((2048, 384), (4096, 0), (1024, 192)):
                meminfo.write_text(f"MemTotal: {ram * 1024} kB\n")
                first = subprocess.check_output(command)
                self.assertEqual(json.loads(first)["panel_memory_mib"], expected)
                self.assertEqual(first, subprocess.check_output(command))

    def test_adguard_new_config_and_service(self):
        script = stage("22-adguard.sh")
        config = script.split('cat > "$AGH_CFG" <<YAML\n')[1].split('\nYAML')[0]
        unit = script.split('cat > /etc/systemd/system/adguardhome.service <<EOF\n')[1].split('\nEOF')[0]
        for ram, size, enabled, limits in (
                (2048, 100, False, ("192M", "256M", "160MiB", "0")),
                (4096, 1000, True, ("infinity", "infinity", "off", "infinity"))):
            rendered = render(ram, 8192, "agh") + "\nAGH_PORT=3000\nMGMT_IP=127.0.0.2\n"
            result = subprocess.run(["bash", "-c", rendered + "\ncat <<YAML\n" + config + "\nYAML"],
                                    check=True, capture_output=True, text=True)
            cfg = yaml.safe_load(result.stdout)
            self.assertEqual(cfg["querylog"]["size_memory"], size)
            self.assertEqual(cfg["filters"][1]["enabled"], enabled)
            result = subprocess.run(["bash", "-c", rendered + "\ncat <<EOF\n" + unit + "\nEOF"],
                                    check=True, capture_output=True, text=True)
            for key, value in zip(("MemoryHigh", "MemoryMax", "Environment=GOMEMLIMIT", "MemorySwapMax"), limits):
                self.assertIn(f"{key}={value}", result.stdout)

    def test_adguard_resize_idempotency_and_user_changes(self):
        resource = module()
        with tempfile.TemporaryDirectory() as directory:
            path, state = Path(directory) / "agh.yaml", Path(directory) / "state.json"
            cfg = {"filters": [{"id": 2, "enabled": False, "url": resource.ADAWAY_URL},
                               {"id": 42, "enabled": True, "url": "https://example.org/filter"}],
                   "user_rules": ["@@||example.org^"], "dns": {"cache_size": 123456},
                   "querylog": {"size_memory": 100, "enabled": False, "interval": "24h"}}
            path.write_text(yaml.safe_dump(cfg))
            small, large = resource.calculate(2048, 8192, {}), resource.calculate(8192, 8192, {})
            resource.patch_adguard(path, state, small, {}, fresh=True)
            self.assertTrue(resource.patch_adguard(path, state, large, {}, check=True))
            self.assertEqual(yaml.safe_load(path.read_text()), cfg)
            self.assertTrue(resource.patch_adguard(path, state, large, {}))
            large_cfg = yaml.safe_load(path.read_text())
            self.assertEqual(large_cfg["querylog"]["size_memory"], 1000)
            self.assertTrue(large_cfg["filters"][0]["enabled"])
            self.assertTrue(resource.patch_adguard(path, state, small, {}))
            self.assertEqual(yaml.safe_load(path.read_text()), cfg)
            before = (path.stat().st_mtime_ns, state.stat().st_mtime_ns)
            self.assertFalse(resource.patch_adguard(path, state, small, {}))
            self.assertEqual(before, (path.stat().st_mtime_ns, state.stat().st_mtime_ns))
            cfg["querylog"]["size_memory"] = 77
            cfg["filters"][0]["enabled"] = True
            path.write_text(yaml.safe_dump(cfg))
            resource.patch_adguard(path, state, small, {})
            resource.patch_adguard(path, state, large, {})
            self.assertEqual(yaml.safe_load(path.read_text()), cfg)
            explicit = {"querylog_size": 0, "adaway_enabled": False}
            resource.patch_adguard(path, state, resource.calculate(8192, 8192, explicit), explicit)
            cfg["querylog"]["size_memory"] = 0
            cfg["filters"][0]["enabled"] = False
            self.assertEqual(yaml.safe_load(path.read_text()), cfg)
            # Удаление override возвращает автоматику. Удалённый в UI список
            # затем не воскрешаем даже при явном adaway_enabled.
            resource.patch_adguard(path, state, large, {})
            cfg = yaml.safe_load(path.read_text())
            self.assertEqual(cfg["querylog"]["size_memory"], 1000)
            self.assertTrue(cfg["filters"][0]["enabled"])
            cfg["filters"] = cfg["filters"][1:]
            path.write_text(yaml.safe_dump(cfg))
            resource.patch_adguard(path, state, large, {"adaway_enabled": True})
            self.assertEqual(yaml.safe_load(path.read_text()), cfg)

    def test_legacy_config_and_custom_filter_are_preserved(self):
        resource = module()
        for size in (None, 0, 50, 100, 1000):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                path, state = Path(directory) / "agh.yaml", Path(directory) / "state.json"
                cfg = {"filters": [{"id": 2, "url": "https://example.org/custom", "enabled": True}],
                       "querylog": {"enabled": False}}
                if size is not None:
                    cfg["querylog"]["size_memory"] = size
                path.write_text(yaml.safe_dump(cfg))
                resource.patch_adguard(path, state, resource.calculate(2048, 8192, {}), {})
                if size is None:
                    cfg["querylog"]["size_memory"] = 100
                self.assertEqual(yaml.safe_load(path.read_text()), cfg)


if __name__ == "__main__":
    unittest.main()
