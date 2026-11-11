"""Приоритет юнитов entry: защита sing-box и charon, приглушённый фон.

Контракт:
- `entry_resources.py --ram-mib R --free-mib F --unit <юнит>` печатает тело
  drop-in `[Service]` для юнита (имя без `.service`) по ступени памяти;
- `entry_unit_resources <юнит>` из infra/lib/entry-resources.sh пишет его в
  `${ENTRY_UNIT_DIR:-/etc/systemd/system}/<юнит>.service.d/zz-vpn2-resources.conf`;
- стадия, ставящая юнит, зовёт `entry_unit_resources <юнит>`.
charon - пакетный strongswan-starter (стадия 27).

ENTRY_MEMORY_ROOT - дерево для регрессии на другом коммите.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(os.environ.get("ENTRY_MEMORY_ROOT", Path(__file__).resolve().parents[2]))
TOOL = ROOT / "infra/lib/entry_resources.py"
LIB = ROOT / "infra/lib/entry-resources.sh"
ROLES = ROOT / "infra/ansible/roles"
DROPIN = "zz-vpn2-resources.conf"
PROTECTED = {"sing-box": ("role:router", "role:exit"),
             "strongswan-starter": ("role:ikev2",)}
BACKGROUND = {"anysda-yt-failover": "role:zapret",
              "anysda-failover-watchdog": "role:failover",
              "anysda-tproxy-watchdog": "role:failover",
              "vpn2-emergency-snapshot": "role:emergency"}
EXIT_ONLY = {"role:exit"}
HEREDOC = re.compile(r"""^\s*cat\s+>\s*(\S+)\s*<<-?\s*['"]?(\w+)['"]?\s*$""")
INSTALL = re.compile(r"""^\s*install\s.*?([\w@.-]+\.service)"?\s+/etc/systemd/system/?\s*$""")


def unit_files():
    out = subprocess.run(["git", "-C", str(ROOT), "grep", "-ln", r"\[Service\]", "--",
                          "infra/scripts", "infra/lib"], capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.split()]


def directives(text):
    """Последнее значение каждого ключа секции [Service]; пустое присваивание сбрасывает."""
    section, result = None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Service]" and "=" in line and not line.startswith(("#", ";")):
            key, value = line.split("=", 1)
            if value.strip():
                result[key.strip()] = value.strip()
            else:
                result.pop(key.strip(), None)
    return result


def size_mib(value, ram):
    value = value.strip()
    if value == "infinity":
        return float("inf")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT%]?)", value)
    if not match:
        raise AssertionError(f"не размер: {value!r}")
    number, suffix = float(match[1]), match[2]
    if suffix == "%":
        return ram * number / 100
    return number * {"": 1 / 2**20, "K": 1 / 1024, "M": 1, "G": 1024, "T": 2**20}[suffix]


def inventory():
    """[(юнит, скрипт, строка, тело)]; число [Service] в файле сверяется с найденным."""
    units, mismatch = [], []
    for path in unit_files():
        text = path.read_text()
        lines = text.splitlines()
        found, i = 0, 0
        while i < len(lines):
            match = HEREDOC.match(lines[i])
            install = INSTALL.match(lines[i])
            if match:
                target, delim = match[1].strip('"'), match[2]
                end = next(j for j in range(i + 1, len(lines)) if lines[j].strip() == delim)
                body = "\n".join(lines[i + 1:end])
                found += sum(1 for line in body.splitlines() if line.strip() == "[Service]")
                var = re.fullmatch(r"\$\{?(\w+)\}?", target)
                if var:
                    target = re.search(rf"^\s*{var[1]}=(\S+)", text, re.M)[1].strip('"')
                if target.endswith(".service") and ".d/" not in target and "[Service]" in body:
                    units.append((Path(target).stem, path.name, i + 1, body))
                i = end
            elif install:
                source = next(ROOT.glob(f"infra/**/{install[1]}"))
                units.append((Path(install[1]).stem, path.name, i + 1, source.read_text()))
            i += 1
        total = sum(1 for line in lines if line.strip() == "[Service]")
        if total != found:
            mismatch.append(f"{path.name}: [Service] {total}, разобрано {found}")
    for path in sorted(ROLES.glob("*/files/*.service")):
        units.append((path.stem, f"role:{path.parts[-3]}", 1, path.read_text()))
    for path in sorted(ROLES.glob("*/templates/*.service.j2")):
        units.append((path.name.removesuffix(".service.j2"), f"role:{path.parts[-3]}", 1, path.read_text()))
    return units, mismatch


def role_dropin_units(role):
    """Юниты, которым задачи роли зовут entry_unit_resources из подключённой библиотеки."""
    units = []
    tasks = [inner for path in sorted((ROLES / role / "tasks").glob("*.yml"))
             for task in yaml.safe_load(path.read_text()) for inner in task.get("block", [task])]
    for task in tasks:
        argv = (task.get("ansible.builtin.command") or {}).get("argv") or []
        code = next((i for i, word in enumerate(argv) if 'source "$0/entry-resources.sh"' in str(word)), None)
        if code is None:
            continue
        units += re.findall(r"entry_unit_resources ([\w@.-]+)", argv[code])
        if re.search(r'entry_unit_resources "\$', argv[code]):
            for word in argv[code + 2:]:
                units += (task.get("loop") or []) if word == "{{ item }}" else [word]
    return units


def calls_dropin(script, unit):
    if script.startswith("role:"):
        return unit in role_dropin_units(script[5:])
    text = (ROOT / "infra/scripts" / script).read_text()
    return re.search(rf"""^\s*entry_unit_resources\s+['"]?{re.escape(unit)}(\.service)?['"]?\s*$""",
                     text, re.M) is not None


def dropin(unit, ram, disk=8192):
    result = subprocess.run([sys.executable, str(TOOL), "--ram-mib", str(ram), "--free-mib", str(disk),
                             "--unit", unit], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(f"--unit {unit}: код {result.returncode}: {result.stderr.strip()[-300:]}")
    return directives(result.stdout)


def effective(unit, script, body, ram):
    values = directives(body)
    if calls_dropin(script, unit):
        values.update(dropin(unit, ram))
    return values


def guarded(values, ram):
    if "MemoryMax" in values or "MemoryHigh" in values:
        return True
    try:
        return (int(values.get("OOMScoreAdjust", "0")) < 0
                and size_mib(values.get("MemoryLow", "0"), ram) > 0)
    except (ValueError, AssertionError):
        return False


class EntryUnitPriorityTest(unittest.TestCase):
    def test_inventory_every_unit_limited_or_protected(self):
        units, mismatch = inventory()
        self.assertEqual(mismatch, [], "юнит записан не heredoc'ом - опись его не видит")
        self.assertGreater(len(units), 20)
        bad = []
        for unit, script, line, body in units:
            for ram in (2048, 8192):
                if not guarded(effective(unit, script, body, ram), ram):
                    bad.append(f"{unit} ({script}:{line}, {ram} MiB)")
        self.assertEqual(bad, [], "юнит без MemoryMax/MemoryHigh и без OOMScoreAdjust<0+MemoryLow")

    def test_router_and_charon_protected(self):
        for unit, scripts in PROTECTED.items():
            for script in scripts:
                with self.subTest(unit=unit, script=script):
                    self.assertTrue(calls_dropin(script, unit), f"{script} не зовёт entry_unit_resources {unit}")
                    if not script.startswith("role:"):
                        self.assertIn('source "$RESOURCE_LIB"', (ROOT / "infra/scripts" / script).read_text())
            for ram in (2048, 8192):
                with self.subTest(unit=unit, ram=ram):
                    values = dropin(unit, ram)
                    self.assertEqual(values.get("OOMScoreAdjust"), "-900")
                    self.assertGreater(size_mib(values.get("MemoryLow", "0"), ram), 0)
                    self.assertEqual(values.get("CPUWeight"), "1000")
                    self.assertEqual(values.get("IOWeight"), "1000")

    def test_background_services_deprioritized(self):
        for unit, script in BACKGROUND.items():
            with self.subTest(unit=unit):
                self.assertTrue(calls_dropin(script, unit), f"{script} не зовёт entry_unit_resources {unit}")
                if not script.startswith("role:"):
                    self.assertIn('source "$RESOURCE_LIB"', (ROOT / "infra/scripts" / script).read_text())
                for ram in (2048, 8192):
                    values = dropin(unit, ram)
                    self.assertLess(int(values.get("CPUWeight", "100")), 100, ram)
                    self.assertLess(int(values.get("IOWeight", "100")), 100, ram)
                    self.assertGreater(int(values.get("Nice", "0")), 0, ram)

    def test_memory_numbers_follow_tier(self):
        for unit in PROTECTED:
            with self.subTest(unit=unit):
                small = size_mib(dropin(unit, 2048).get("MemoryLow", "0"), 2048)
                large = size_mib(dropin(unit, 8192).get("MemoryLow", "0"), 8192)
                self.assertGreater(small, 0)
                self.assertLess(small, 2048)
                self.assertGreaterEqual(large, small)
        capped = {}
        for unit in BACKGROUND:
            values = dropin(unit, 2048)
            self.assertIn("MemoryMax", values, unit)
            capped[unit] = None
        units, _ = inventory()
        for unit, script, _line, _body in units:
            if script not in EXIT_ONLY and calls_dropin(script, unit) and "MemoryMax" in dropin(unit, 2048):
                capped[unit] = None
        total = 0
        for unit in capped:
            with self.subTest(unit=unit):
                small = size_mib(dropin(unit, 2048)["MemoryMax"], 2048)
                large = size_mib(dropin(unit, 8192).get("MemoryMax", "infinity"), 8192)
                self.assertNotEqual(small, float("inf"))
                self.assertGreater(small, 0)
                self.assertGreaterEqual(large, small)
                total += small
        budget = json.loads(subprocess.check_output(
            [sys.executable, str(TOOL), "--ram-mib", "2048", "--free-mib", "8192"]))
        total += (budget["panel_memory_mib"] + budget["bot_memory_mib"] + budget["vm_memory_mib"]
                  + 2 * budget["agh_memory_mib"])
        self.assertLessEqual(total, 2048, f"сумма потолков на 2 ГБ: {total:.0f} MiB")

    def test_redeploy_over_manual_dropin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit_dir = root / "sing-box.service.d"
            unit_dir.mkdir()
            manual = unit_dir / "override.conf"
            manual.write_bytes(b"[Service]\nMemoryLow=50M\nOOMScoreAdjust=-500\n")
            stamp = manual.stat().st_mtime_ns
            command = ("systemctl() { :; }; "
                       f"source {shlex.quote(str(LIB))}; entry_unit_resources sing-box")
            env = dict(os.environ, ENTRY_UNIT_DIR=str(root))

            def run():
                result = subprocess.run(["bash", "-eu", "-c", command], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr.strip()[-300:])

            run()
            ours = unit_dir / DROPIN
            self.assertTrue(ours.is_file(), f"нет {DROPIN}")
            self.assertEqual(sorted(p.name for p in unit_dir.iterdir()), ["override.conf", DROPIN])
            self.assertEqual(manual.read_bytes(), b"[Service]\nMemoryLow=50M\nOOMScoreAdjust=-500\n")
            self.assertEqual(manual.stat().st_mtime_ns, stamp)
            first = ours.read_bytes()
            run()
            self.assertEqual(ours.read_bytes(), first)
            merged = directives(manual.read_text())
            merged.update(directives(ours.read_text()))
            self.assertEqual(merged.get("OOMScoreAdjust"), "-900")
            self.assertEqual(merged.get("MemoryLow"), directives(ours.read_text()).get("MemoryLow"))
            self.assertNotEqual(merged.get("MemoryLow"), "50M")


if __name__ == "__main__":
    unittest.main()
