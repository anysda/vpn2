"""infra/lib/ansible-venv.sh: хэш пинов и сборка колеса vpn2 без сети.

Хэш пинов меняется от правки каждого входящего файла и не меняется от
__pycache__. Колесо собирается в VPN2_ROOT, дерево репы остаётся чистым,
повтор при тех же исходниках не пересобирает, правка исходника пересобирает.
pip и galaxy здесь не зовутся.
"""
import glob
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'infra/lib/ansible-venv.sh'
PIN_FILES = ('ansible/constraints.txt', 'ansible/requirements.yml')
OTHER_FILES = ('versions.yml', 'vpn2/pyproject.toml', 'vpn2/src/vpn2/config.py')


def _system_python_builds_wheels():
    """Системный python3 собирает колесо: setuptools-*.whl с bdist_wheel или модуль wheel."""
    found = glob.glob('/usr/share/python-wheels/setuptools-*.whl')
    if not found or not os.access('/usr/bin/python3', os.X_OK):
        return False
    with zipfile.ZipFile(found[0]) as z:
        if 'setuptools/command/bdist_wheel.py' in z.namelist():
            return True
    return subprocess.run(['/usr/bin/python3', '-I', '-c', 'import wheel'],
                          capture_output=True).returncode == 0


NEEDS_WHEEL = unittest.skipUnless(
    _system_python_builds_wheels(),
    'системный python3 не собирает колёса (python3-venv, python3-wheel)')
NEEDS_PIP_WHEEL = unittest.skipUnless(
    glob.glob('/usr/share/python-wheels/pip-*.whl'),
    'нет системного pip-*.whl (python3-venv)')


def bash(body, **env):
    """Запуск функции lib в bash; log и die даёт вызывающий."""
    script = ('log() { echo "$*"; }; die() { echo "$*" >&2; exit 1; }\n'
              f'source {LIB}\n{body}\n')
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                          env={**os.environ, **env})


def build(test, tmp_path, infra_dir=None):
    env = {'VPN2_ROOT': str(tmp_path / 'root')}
    if infra_dir:
        env['ANSIBLE_VENV_INFRA'] = str(infra_dir)
    res = bash('ansible_wheel_build', **env)
    test.assertEqual(res.returncode, 0, res.stderr)
    wheels = sorted((tmp_path / 'root/wheels').glob('vpn2-*-py3-none-any.whl'))
    test.assertEqual(len(wheels), 1, wheels)
    return wheels[0]


def tree_status():
    res = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', 'infra/vpn2'],
                         capture_output=True, text=True)
    return res.stdout


def install(tmp_path, bin_dir, infra_dir, **env):
    return bash('ansible_venv_install',
                VPN2_ROOT=str(tmp_path / 'root'), ANSIBLE_VENV_INFRA=str(infra_dir),
                VPN2_PYTHON=str(bin_dir / 'python3'), **env)


class AnsibleVenvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.new_tmp()

    def new_tmp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def infra(self, tmp_path=None):
        """Копия входящих файлов infra: правка не трогает настоящее дерево."""
        dst = (tmp_path or self.tmp) / 'infra'
        shutil.copytree(ROOT / 'infra/vpn2', dst / 'vpn2',
                        ignore=shutil.ignore_patterns('__pycache__', 'tests', '*.egg-info'))
        for name in PIN_FILES + OTHER_FILES:
            (dst / name).parent.mkdir(parents=True, exist_ok=True)
            src = ROOT / 'infra' / name
            if src.exists():
                shutil.copy(src, dst / name)
            else:
                (dst / name).write_text('x\n')
        return dst

    def pins(self, infra_dir):
        res = bash('ansible_venv_pins', ANSIBLE_VENV_INFRA=str(infra_dir))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.strip()), 64)
        return res.stdout.strip()

    def fake_python(self):
        """Заглушка python3 для VPN2_PYTHON: пишет аргументы в файл, pip и сеть не зовутся."""
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        calls = self.tmp / 'calls'
        stub = bin_dir / 'python3'
        # `-m venv <каталог>` оставляет каталог с bin/vpn2 и bin/ansible-galaxy;
        # вызов с подстрокой из FAIL_ON падает - так обрывается сборка.
        stub.write_text('#!/bin/sh\n'
                        'if [ "$1" = "-V" ]; then echo "Python 3.0.0"; exit 0; fi\n'
                        f'echo "$*" >> {calls}\n'
                        'if [ -n "$FAIL_ON" ]; then case "$*" in *"$FAIL_ON"*) exit 1;; esac; fi\n'
                        '[ "$1" = "-m" ] || exit 0\n'
                        'for dir; do :; done\n'
                        'mkdir -p "$dir/bin"\n'
                        f'printf \'#!/bin/sh\\necho "galaxy $*" >> {calls}\\n\' > "$dir/bin/ansible-galaxy"\n'
                        'printf \'#!/bin/sh\\necho vpn2\\n\' > "$dir/bin/vpn2"\n'
                        'chmod +x "$dir/bin/ansible-galaxy" "$dir/bin/vpn2"\n')
        stub.chmod(0o755)
        return bin_dir, calls

    def edit_source(self, infra):
        with open(infra / 'vpn2/src/vpn2/config.py', 'a') as f:
            f.write('\n# правка\n')
        build(self, self.tmp, infra)

    def installed(self, bin_dir, infra, **env):
        res = install(self.tmp, bin_dir, infra, **env)
        self.assertEqual(res.returncode, 0, res.stderr)
        venv = self.tmp / 'root/venv'
        self.assertTrue(venv.is_symlink(), 'venv - симлинк на сборку')
        self.assertEqual(subprocess.run([venv / 'bin/vpn2'], capture_output=True, text=True).stdout,
                         'vpn2\n')
        return res, venv.resolve()

    def venv_wait(self, mode):
        log = self.tmp / 'venv.log'
        return bash(
            'warn() { echo "WARN $*" >&2; }\n'
            f'echo pip-boom > {log}; (exit 1) & _VENV_PID=$!\n'
            f'ansible_venv_wait {mode}; echo "rc=$?"',
            VPN2_ROOT=str(self.tmp / 'root'), VENV_LOG=str(log))

    def test_pins_change_with_each_input(self):
        for name in PIN_FILES:
            with self.subTest(name=name):
                infra = self.infra(self.new_tmp())
                before = self.pins(infra)
                with open(infra / name, 'a') as f:
                    f.write('\n# правка\n')
                self.assertNotEqual(self.pins(infra), before)

    def test_pins_ignore_versions_and_package_sources(self):
        for name in OTHER_FILES:
            with self.subTest(name=name):
                infra = self.infra(self.new_tmp())
                before = self.pins(infra)
                with open(infra / name, 'a') as f:
                    f.write('\n# правка\n')
                self.assertEqual(self.pins(infra), before)

    def test_pins_ignore_pycache(self):
        infra = self.infra()
        before = self.pins(infra)
        cache = infra / 'vpn2/src/vpn2/__pycache__'
        cache.mkdir()
        (cache / 'config.cpython-314.pyc').write_bytes(b'junk')
        (infra / 'vpn2/src/vpn2/stale.pyc').write_bytes(b'junk')
        self.assertEqual(self.pins(infra), before)

    @NEEDS_WHEEL
    def test_wheel_build_leaves_tree_clean(self):
        # Сборка ничего не добавляет к тому, что уже было в дереве до неё.
        before = tree_status()
        wheel = build(self, self.tmp)
        self.assertGreater(wheel.stat().st_size, 0)
        self.assertTrue((wheel.parent / '.src-sha256').is_file())
        self.assertEqual(tree_status(), before)

    @NEEDS_WHEEL
    def test_second_build_does_not_rebuild(self):
        wheel = build(self, self.tmp)
        first = wheel.stat().st_mtime_ns
        build(self, self.tmp)
        self.assertEqual(wheel.stat().st_mtime_ns, first)

    @NEEDS_WHEEL
    def test_source_edit_rebuilds(self):
        infra = self.infra()
        wheel = build(self, self.tmp, infra)
        first = wheel.stat().st_mtime_ns
        with open(infra / 'vpn2/src/vpn2/config.py', 'a') as f:
            f.write('\n# правка\n')
        wheel = build(self, self.tmp, infra)  # в каталоге по-прежнему одно колесо
        self.assertNotEqual(wheel.stat().st_mtime_ns, first)

    @NEEDS_WHEEL
    def test_wheel_path(self):
        env = {'VPN2_ROOT': str(self.tmp / 'root')}
        res = bash('ansible_wheel_path', **env)
        self.assertTrue(res.returncode == 1 and res.stdout == '')
        wheel = build(self, self.tmp)
        res = bash('ansible_wheel_path', **env)
        self.assertTrue(res.returncode == 0 and res.stdout.strip() == str(wheel))
        self.assertTrue(wheel.name.startswith('vpn2-')
                        and wheel.name.endswith('-py3-none-any.whl'))
        self.assertFalse((wheel.parent / 'vpn2.whl').exists())
        self.assertFalse((wheel.parent / '.name').exists())

    @NEEDS_WHEEL
    @NEEDS_PIP_WHEEL
    def test_install_branches(self):
        infra = self.infra()
        bin_dir, calls = self.fake_python()
        build(self, self.tmp, infra)
        root = self.tmp / 'root'
        # первая установка: загрузка колёс в склад, сборка рядом, коллекции
        _, first = self.installed(bin_dir, infra)
        lines = calls.read_text().splitlines()
        self.assertEqual([' download ' in x for x in lines], [True, False, False, False, False])
        self.assertTrue(lines[1].startswith('-m venv --without-pip')
                        and '--clear' not in lines[1])
        self.assertTrue('--no-index --find-links' in lines[2] and lines[2].endswith('ansible-core'))
        self.assertTrue('--no-index --no-deps' in lines[3] and lines[4].startswith('galaxy '))
        self.assertEqual(first.parent, (root / 'venvs').resolve())
        # те же пины и колесо: ни одного вызова, сборка та же
        res, same = self.installed(bin_dir, infra)
        self.assertIn('пины те же, без загрузки', res.stdout)
        self.assertEqual(calls.read_text().splitlines(), lines)
        self.assertEqual(same, first)
        # другое колесо: копия действующей сборки и одна установка колеса, без сети
        (first / 'bin/tool').write_text(f'#!{first}/bin/python\n')
        self.edit_source(infra)
        res, second = self.installed(bin_dir, infra)
        self.assertIn('колесо vpn2 обновлено', res.stdout)
        added = calls.read_text().splitlines()[len(lines):]
        self.assertEqual(len(added), 1, added)
        self.assertTrue('--no-index --no-deps --force-reinstall' in added[0], added)
        self.assertNotEqual(second, first)
        self.assertEqual([x.name for x in (root / 'venvs').iterdir()], [second.name])
        # запускалки копии смотрят на свою сборку, а не на снесённую
        self.assertEqual((second / 'bin/tool').read_text(), f'#!{second}/bin/python\n')
        self.assertTrue((root / 'venv/.vpn2-wheel').read_text().startswith(
            (root / 'wheels/.src-sha256').read_text().strip()))
        # другие пины: склад и коллекции качаются заново
        lines = calls.read_text().splitlines()
        stamp = (root / 'venv/.vpn2-wheel').read_text()
        with open(infra / 'ansible/constraints.txt', 'a') as f:
            f.write('\n# правка\n')
        res, third = self.installed(bin_dir, infra)
        added = calls.read_text().splitlines()[len(lines):]
        self.assertTrue(' download ' in added[0] and added[-1].startswith('galaxy '), added)
        self.assertTrue(any(x.startswith('-m venv') for x in added), added)
        self.assertEqual([x.name for x in (root / 'venvs').iterdir()], [third.name])
        # Отметка, по которой роли перезапускают службы пакета, меняется и от
        # одних пинов: иначе демон остаётся на сборке с прежними зависимостями.
        self.assertNotEqual((root / 'venv/.vpn2-wheel').read_text(), stamp)

    @NEEDS_WHEEL
    @NEEDS_PIP_WHEEL
    def test_aborted_copy_is_rebuilt(self):
        infra = self.infra()
        bin_dir, calls = self.fake_python()
        build(self, self.tmp, infra)
        root = self.tmp / 'root'
        _, first = self.installed(bin_dir, infra)
        self.edit_source(infra)
        # Обрыв между копией и установкой колеса: копия без метки, с прежним пакетом.
        res = install(self.tmp, bin_dir, infra, FAIL_ON='--force-reinstall')
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual((root / 'venv').resolve(), first)
        left = [x for x in (root / 'venvs').iterdir() if x != first]
        self.assertFalse(any((x / '.vpn2-pins').exists() for x in left), left)
        before = len(calls.read_text().splitlines())
        _, second = self.installed(bin_dir, infra)
        self.assertEqual(len(calls.read_text().splitlines()), before + 1)
        self.assertEqual([x.name for x in (root / 'venvs').iterdir()], [second.name])

    @NEEDS_WHEEL
    @NEEDS_PIP_WHEEL
    def test_aborted_build_leaves_current_venv_working(self):
        infra = self.infra()
        bin_dir, _ = self.fake_python()
        build(self, self.tmp, infra)
        root = self.tmp / 'root'
        _, first = self.installed(bin_dir, infra)
        marks = [(root / 'venv' / x).read_text() for x in ('.vpn2-pins', '.vpn2-wheel')]
        self.edit_source(infra)
        res = install(self.tmp, bin_dir, infra, FAIL_ON='--no-deps')
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual((root / 'venv').resolve(), first)
        self.assertEqual([(root / 'venv' / x).read_text() for x in ('.vpn2-pins', '.vpn2-wheel')],
                         marks)
        self.assertEqual(subprocess.run([root / 'venv/bin/vpn2'], capture_output=True).returncode, 0)
        # повтор: оборванная сборка снесена и собрана заново
        _, second = self.installed(bin_dir, infra)
        self.assertNotEqual(second, first)
        self.assertEqual([x.name for x in (root / 'venvs').iterdir()], [second.name])

    @NEEDS_WHEEL
    @NEEDS_PIP_WHEEL
    def test_directory_of_old_release_becomes_link(self):
        infra = self.infra()
        bin_dir, calls = self.fake_python()
        build(self, self.tmp, infra)
        root = self.tmp / 'root'
        venv = root / 'venv'
        venv.mkdir(parents=True)
        pin = bash('ansible_venv_pins', ANSIBLE_VENV_INFRA=str(infra),
                   VPN2_PYTHON=str(bin_dir / 'python3')).stdout.strip()
        (venv / '.vpn2-pins').write_text(pin + '\n')
        (venv / '.vpn2-wheel').write_text(
            f"{(root / 'wheels/.src-sha256').read_text().strip()} {pin}\n")
        # метки те же: каталог прежнего выпуска не трогается
        res = install(self.tmp, bin_dir, infra)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('пины те же, без загрузки', res.stdout)
        self.assertFalse(calls.exists())
        self.assertFalse(venv.is_symlink())
        # другое колесо: каталог уступает место симлинку, коллекции не качаются
        self.edit_source(infra)
        self.installed(bin_dir, infra)
        self.assertFalse(any(x.startswith('galaxy ') for x in calls.read_text().splitlines()))
        self.assertEqual(len(list((root / 'venvs').iterdir())), 1)
        self.assertEqual(sorted(x.name for x in root.iterdir() if x.name.startswith('venv')),
                         ['venv', 'venvs'])

    @NEEDS_WHEEL
    @NEEDS_PIP_WHEEL
    def test_build_of_running_process_is_kept(self):
        infra = self.infra()
        bin_dir, _ = self.fake_python()
        build(self, self.tmp, infra)
        root = self.tmp / 'root'
        _, first = self.installed(bin_dir, infra)
        # Командная строка службы из venv: настоящий путь сборки.
        service = subprocess.Popen(['python3', '-c', 'import time; time.sleep(120)',
                                    f'{first}/bin/vpn2'])
        try:
            self.edit_source(infra)
            _, second = self.installed(bin_dir, infra)
            self.assertTrue(first.is_dir(), 'сборка живого процесса снесена')
        finally:
            service.kill()
            service.wait()
        self.installed(bin_dir, infra)
        self.assertEqual([x.name for x in (root / 'venvs').iterdir()], [second.name])

    def test_background_install_does_not_hold_deploy_lock(self):
        # Деплой берёт замок через exec {fd}> и падает; фон с sleep ещё идёт,
        # а следующий запуск обязан взять тот же замок.
        lock = self.tmp / 'deploy.lock'
        res = bash(
            f'exec {{fd}}>{lock}; flock -n "$fd" || exit 9\n'
            'ansible_wheel_build() { :; }\n'
            '_ansible_venv_install_locked() { sleep 60; }\n'
            'ansible_venv_start\n'
            'echo "bg=$_VENV_PID" >&2\n'
            'exit 1',
            VPN2_ROOT=str(self.tmp / 'root'))
        bg = int(res.stderr.rsplit('bg=', 1)[1].split()[0])
        try:
            self.assertEqual(res.returncode, 1, res.stderr)
            # Фон закрывает унаследованный дескриптор замка первым делом, но
            # уже после своего запуска: сразу за выходом деплоя замок ещё может
            # быть занят. Ждём его меньше, чем живёт фон: держи фон замок до
            # конца установки, ожидание не дождалось бы.
            second = subprocess.run(['flock', '-w', '20', str(lock), 'true'])
            self.assertEqual(second.returncode, 0, 'фон venv держит замок деплоя')
        finally:
            subprocess.run(['pkill', '-P', str(bg)])
            subprocess.run(['kill', str(bg)])

    def test_soft_venv_wait_warns_and_returns_zero(self):
        res = self.venv_wait('soft')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('rc=0', res.stdout)
        self.assertIn('pip-boom', res.stderr)
        self.assertIn('WARN ansible: venv не установился, лог', res.stderr)
        self.assertIn('повторится при следующем запуске', res.stderr)

    def test_strict_venv_wait_dies_on_failed_background(self):
        res = self.venv_wait('')
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn('rc=', res.stdout)
        self.assertIn('ansible: venv не установился', res.stderr)

    @NEEDS_WHEEL
    def test_wheel_build_installs_venv_package_only_when_setuptools_missing(self):
        system = glob.glob('/usr/share/python-wheels/setuptools-*.whl')[0]
        for ready in (False, True):
            with self.subTest(ready=ready):
                tmp = self.new_tmp()
                wheels = tmp / 'pywheels'
                wheels.mkdir()
                if ready:
                    shutil.copy(system, wheels)
                calls = tmp / 'calls'
                res = bash(
                    f'_install_prereq_pkgs() {{ echo "$*" >> {calls}; '
                    f'cp {system} "$PYTHON_WHEELS_DIR/"; }}\n'
                    'ansible_wheel_build',
                    VPN2_ROOT=str(tmp / 'root'), PYTHON_WHEELS_DIR=str(wheels))
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertTrue(list((tmp / 'root/wheels').glob('vpn2-*-py3-none-any.whl')))
                if ready:
                    self.assertFalse(calls.exists())
                else:
                    self.assertEqual(calls.read_text().splitlines(), ['python3-venv'])

    def test_wheel_build_installs_wheel_package_only_when_setuptools_lacks_bdist_wheel(self):
        for has_cmd in (False, True):
            with self.subTest(has_bdist_wheel=has_cmd):
                tmp = self.new_tmp()
                wheels = tmp / 'pywheels'
                wheels.mkdir()
                with zipfile.ZipFile(wheels / 'setuptools-1-py3-none-any.whl', 'w') as z:
                    z.writestr('setuptools/__init__.py', '')
                    if has_cmd:
                        z.writestr('setuptools/command/__init__.py', '')
                        z.writestr('setuptools/command/bdist_wheel.py', '')
                calls = tmp / 'calls'
                # Модуль wheel на хосте тестов не мешает: проба его не видит.
                bash(
                    f'_install_prereq_pkgs() {{ echo "$*" >> {calls}; }}\n'
                    'nowheel() { [[ "$*" == *"import wheel"* ]] && return 1; '
                    '/usr/bin/python3 "$@"; }\n'
                    'ansible_wheel_build',
                    VPN2_ROOT=str(tmp / 'root'), PYTHON_WHEELS_DIR=str(wheels),
                    VPN2_PYTHON='nowheel')
                if has_cmd:
                    self.assertFalse(calls.exists())
                else:
                    self.assertEqual(calls.read_text().splitlines(), ['python3-wheel'])


if __name__ == '__main__':
    unittest.main()
