"""VPN2-637: с одним экзитом мёртвый канал не лечится переключением.

Если у всех включённых узлов мертвы все выходы RESTART_AFTER_CHECKS проверок
подряд, сторож перезапускает sing-box (обёртка _restart_sing_box), не чаще
раза в RESTART_MIN_INTERVAL_S, пишет об этом в журнал и шлёт событие в бот
(обёртка _bot_event). Время перезапуска лежит в файле рядом с LANE_STATE_FILE
и переживает перезапуск сторожа. config.yaml: failover.restart_after_checks,
failover.restart_min_interval (секунды); стадия 21 кладёт их в env сторожа.

Сторож гоняется настоящим main(): подменены clash-api (_req), часы и sleep.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import test_config_validation as validation


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))


class Stop(BaseException):
    """Конец прогона: main() ловит Exception, а не BaseException."""


class Clash:
    """clash-api роутера: selector-группы и delay-проба по живым выходам."""

    def __init__(self, nodes, lanes=()):
        self.tags = [f'hy2-{n}-{k}' for n in nodes for k in ('direct', 'warp')]
        self.alive = set()
        self.down = False
        self.now = {'foreign-best': self.tags[0]}
        self.now.update({lane: self.tags[0] for lane in lanes})
        self.puts = []

    def proxy(self, name):
        return {'type': 'Selector', 'now': self.now[name], 'all': list(self.tags)}

    def req(self, method, path, body=None, **_kw):
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))
        path = urllib.parse.unquote(path.split('?')[0])
        name = path.split('/')[2] if path.startswith('/proxies/') else ''
        if method == 'PUT':
            self.now[name] = body['name']
            self.puts.append((name, body['name']))
            return {}
        if method == 'DELETE' or path == '/connections':
            return {'connections': []}
        if path.endswith('/delay'):
            if name in self.alive:
                return {'delay': 100}
            raise urllib.error.URLError(socket.timeout('timed out'))
        if path == '/proxies':
            return {'proxies': {n: self.proxy(n) for n in self.now}}
        return self.proxy(name)


class Clock:
    """Одни часы на monotonic и time; проверка = sleep(INTERVAL) в main()."""

    def __init__(self, interval, mono=1000.0, wall=1_800_000_000.0):
        self.interval = interval
        self.mono, self.shift = mono, wall - mono
        self.checks = 0
        self.limit = 0
        self.on_check = None

    def monotonic(self):
        return self.mono

    def time(self):
        return self.mono + self.shift

    def sleep(self, seconds):
        self.mono += seconds
        if seconds != self.interval:
            return   # до-проверка внутри тика
        self.checks += 1
        if self.on_check:
            self.on_check(self.checks)
        if self.checks >= self.limit:
            raise Stop


def watchdog(state_dir, **env):
    spec = importlib.util.spec_from_file_location('watchdog', ROOT / 'infra/lib/failover-watchdog.py')
    module = importlib.util.module_from_spec(spec)
    base = {'CLASH_SECRET': 'test',
            'LANE_STATE_FILE': str(Path(state_dir) / 'lanes.json'),
            'DISABLED_EXITS_FILE': str(Path(state_dir) / 'exits-disabled.json')}
    with patch.dict(os.environ, {**base, **env}, clear=True):
        spec.loader.exec_module(module)
    return module


class Run:
    """Прогон main() на checks проверок; перезапуски — по настенным часам."""

    def __init__(self, w, clash, clock):
        self.w, self.clash, self.clock = w, clash, clock
        self.restarts = []      # clock.time() каждого перезапуска
        self.restart_check = []  # номер проверки, на которой был перезапуск
        self.events = []
        self.out = io.StringIO()

    def restart(self, *_args, **_kwargs):
        self.restarts.append(self.clock.time())
        self.restart_check.append(self.clock.checks + 1)

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))

    def go(self, checks):
        self.clock.limit = self.clock.checks + checks
        w = self.w
        with patch.object(w, '_req', self.clash.req), \
                patch.object(w.time, 'monotonic', self.clock.monotonic), \
                patch.object(w.time, 'time', self.clock.time), \
                patch.object(w.time, 'sleep', self.clock.sleep), \
                patch.object(w, '_restart_sing_box', side_effect=self.restart, create=True), \
                patch.object(w, '_bot_event', side_effect=self.event, create=True), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.out):
            try:
                w.main()
            except Stop:
                pass
        return self


def dead_run(state_dir, checks, nodes=('aa',), **env):
    w = watchdog(state_dir, **env)
    clash = Clash(nodes)
    return Run(w, clash, Clock(w.INTERVAL)).go(checks)


class TestFailoverRestart(unittest.TestCase):
    # 1
    def test_single_node_all_channels_dead_restarts_sing_box_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = dead_run(tmp, 13)
            self.assertEqual(run.restart_check, [3],
                             'direct и warp единственного узла мертвы 3 проверки подряд: ждём '
                             'один перезапуск sing-box на 3-й проверке и ни одного за 10 следующих, '
                             f'было на проверках {run.restart_check}:\n' + run.out.getvalue()[-600:])
            self.assertEqual(len(run.events), 1, 'событие в бот о перезапуске — ровно одно')
            log = run.out.getvalue().splitlines()[1:]   # без строки запуска
            self.assertTrue(any('sing-box' in line and 'перезапус' in line for line in log),
                            'перезапуск не попал в журнал')

    # 2
    def test_restart_rate_limit_is_ten_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 3 проверки до первого перезапуска и ещё 11 минут мёртвых
            run = dead_run(tmp, 3 + int(660 / 2), INTERVAL='2')
            gaps = [round(b - a) for a, b in zip(run.restarts, run.restarts[1:])]
            self.assertEqual(run.restart_check[:1], [3], 'первого перезапуска нет')
            self.assertEqual(len(gaps), 1, f'за 11 минут после первого ждём ровно второй, '
                                           f'промежутки между перезапусками, с: {gaps}')
            self.assertGreaterEqual(gaps[0], 600, 'второй перезапуск раньше 10 минут')
            self.assertLessEqual(gaps[0], 660, 'на 11-й минуте второго перезапуска нет')

    # 3
    def test_rate_limit_survives_watchdog_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = dead_run(tmp, 3)
            self.assertEqual(len(run.restarts), 1, 'первого перезапуска нет')
            first = run.restarts[0]
            # новый процесс: monotonic с нуля, настенное время идёт дальше
            w = watchdog(tmp)
            clock = Clock(w.INTERVAL, mono=5.0, wall=run.clock.time() + 2)
            again = Run(w, Clash(['aa']), clock).go(int(660 / w.INTERVAL))
            self.assertEqual(len(again.restarts), 1,
                             'после перезапуска сторожа предел частоты сбросился '
                             f'или второго перезапуска нет: {again.restarts}')
            self.assertGreaterEqual(again.restarts[0] - first, 600,
                                    'новый процесс сторожа перезапустил sing-box раньше 10 минут')

    # 4
    def test_dead_streak_must_be_consecutive(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp)
            clash = Clash(['aa'])
            clock = Clock(w.INTERVAL)
            # проверки 1-2 мертвы, 3-я жива, 4-5 мертвы
            clock.on_check = lambda n: setattr(clash, 'alive', set(clash.tags) if n == 2 else set())
            run = Run(w, clash, clock).go(5)
            self.assertEqual(run.restarts, [], 'счётчик мёртвых проверок — подряд, не всего')

    # 5
    def test_one_of_two_nodes_dead_switches_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp)
            clash = Clash(['aa', 'bb'], lanes=['lane-00'])
            clash.alive = {'hy2-bb-direct', 'hy2-bb-warp'}
            run = Run(w, clash, Clock(w.INTERVAL)).go(20)
            self.assertEqual(run.restarts, [], 'жив узел bb — перезапускать нечего')
            self.assertEqual(w._node_of(clash.now['foreign-best']), 'bb')
            self.assertIn(('lane-00', 'hy2-bb-direct'), clash.puts)

    # 6
    def test_disabled_or_penalized_single_node_is_not_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'exits-disabled.json').write_text(json.dumps({'disabled': ['aa']}))
            run = dead_run(tmp, 20)
            self.assertEqual(run.restarts, [], 'единственный узел выключен вручную')
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp)
            clash = Clash(['aa'])
            clash.alive = set(clash.tags)
            clock = Clock(w.INTERVAL)
            w._penalty['aa'] = clock.monotonic() + 600
            run = Run(w, clash, clock).go(20)
            self.assertEqual(run.restarts, [], 'узел в штрафной, но его выходы живы')

    # 7
    def test_clash_api_down_is_not_a_dead_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp)
            clash = Clash(['aa'])
            clash.down = True
            run = Run(w, clash, Clock(w.INTERVAL)).go(20)
            self.assertEqual(run.restarts, [], 'clash-api молчит — это не «канал мёртв»')

    # 8
    def test_thresholds_from_env_and_zero_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = dead_run(tmp, 5 + int(960 / 2), INTERVAL='2',
                           RESTART_AFTER_CHECKS='5', RESTART_MIN_INTERVAL_S='900')
            gaps = [round(b - a) for a, b in zip(run.restarts, run.restarts[1:])]
            self.assertEqual(run.restart_check[:1], [5],
                             f'RESTART_AFTER_CHECKS=5: перезапуск на проверках {run.restart_check}')
            self.assertEqual(len(gaps), 1, f'за 16 минут ждём два перезапуска, промежутки: {gaps}')
            self.assertGreaterEqual(gaps[0], 900, 'RESTART_MIN_INTERVAL_S=900 не соблюдён')
        with tempfile.TemporaryDirectory() as tmp:
            run = dead_run(tmp, 20, RESTART_AFTER_CHECKS='0')
            self.assertEqual(run.restarts, [], 'RESTART_AFTER_CHECKS=0 выключает перезапуск')


def stage_env(failover):
    """config.yaml -> config2env -> env стадии (как run_stage_on_host) ->
    стадия 21 в песочнице -> /etc/anysda/failover-watchdog.env."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = validation.ConfigValidationTests.convert(None, validation.CONFIG + failover, directory)
        if result.returncode:
            return f'config2env: {result.stdout}{result.stderr}'
        envs = root / 'infra/envs'
        rendered = root / 'ru.env'
        rendered.write_text((envs / 'all.env').read_text() + '\n' + ''.join(
            line + '\n' for line in (envs / 'ru.env').read_text().splitlines()
            if not line.startswith('SSH_PASS=')))
        box = root / 'box'

        def jail(text):
            for prefix in ('/etc/', '/var/', '/usr/local/', '/tmp/anysda'):
                text = text.replace(prefix, f'{box}{prefix}')
            return text

        stage = root / 'stage'
        stage.mkdir()
        (stage / '21-failover-watchdog.sh').write_text(
            jail((ROOT / 'infra/scripts/21-failover-watchdog.sh').read_text()))
        (stage / 'anysda-svc.sh').write_text((ROOT / 'infra/lib/anysda-svc.sh').read_text())
        for path in ('etc/anysda', 'etc/systemd/system', 'tmp/anysda', 'usr/local/bin', 'usr/local/sbin'):
            (box / path).mkdir(parents=True)
        (box / 'etc/anysda/clash-secret.txt').write_text('test\n')
        (box / 'tmp/anysda/failover-watchdog.py').write_text('')
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        stubs = {'systemctl': ':', 'sleep': ':',
                 'install': 'cp "${@: -2:1}" "${@: -1}"'}
        for name, body in stubs.items():
            (bin_dir / name).write_text(f'#!/bin/bash\n{body}\n')
            (bin_dir / name).chmod(0o755)
        result = subprocess.run(
            ['bash', str(stage / '21-failover-watchdog.sh'), str(rendered)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                 'SVC_FP_DIR': str(box / 'var/lib/anysda/applied')})
        if result.returncode:
            return f'стадия 21: {result.stdout}{result.stderr}'
        env = {}
        for line in (box / 'etc/anysda/failover-watchdog.env').read_text().splitlines():
            key, _, value = line.partition('=')
            env[key] = value
        return env


class TestFailoverRestartStage(unittest.TestCase):
    # 8
    def test_config_failover_block_reaches_watchdog_env(self):
        env = stage_env('failover:\n  restart_after_checks: 5\n  restart_min_interval: 900\n')
        self.assertIsInstance(env, dict, env)
        self.assertEqual(env.get('RESTART_AFTER_CHECKS'), '5', 'failover.restart_after_checks')
        self.assertEqual(env.get('RESTART_MIN_INTERVAL_S'), '900', 'failover.restart_min_interval')
        env = stage_env('failover:\n  restart_after_checks: 0\n')
        self.assertIsInstance(env, dict, env)
        self.assertEqual(env.get('RESTART_AFTER_CHECKS'), '0', '0 выключает перезапуск')
        env = stage_env('')
        self.assertIsInstance(env, dict, env)
        self.assertEqual(env.get('RESTART_AFTER_CHECKS'), '3', 'нет блока failover — умолчание 3')
        self.assertEqual(env.get('RESTART_MIN_INTERVAL_S'), '600', 'нет блока failover — умолчание 600')


if __name__ == '__main__':
    unittest.main()
