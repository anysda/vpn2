"""Проверка трафика, пока в панели выпускают и удаляют устройства.

Каждое применение панели ставит на сервер её собственный список клиентов:
временного пира проверки `wg syncconf` снимает, сессию IKEv2 с логином не из
списка служба управления рвёт. Раньше проверка принимала это за поломку и
обрывала деплой. Теперь она видит применение по отметке службы управления,
возвращает свой доступ, подключается заново и повторяет пробу. Настоящая
поломка остаётся ✗; помеха при каждой попытке - «не проверено», а не ✓.

Сервер и панель здесь подменены моделью: ход - одно подключение или одна
проба, панель применяет список на заданных ходах.
"""
import importlib.util
import io
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/vpn2/src/vpn2/verify/traffic.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()
OK, FAIL, UNKNOWN = (vt.STATUSES[i] for i in (vt.OK, vt.FAIL, vt.UNKNOWN))
CHECKS = ['подключение', 'маршрут: прямой', 'маршрут: через экзит se', 'ручное правило se',
          'интернет через экзит se', 'DNS AdGuard', 'DNS внешний', 'интернет через экзит',
          'отрицательный контроль', 'отзыв', 'внешняя проба портов']
SOURCE = '10.8.0.250'


class World:
    """Сервер с временными доступами проверки и панель со своим списком клиентов."""

    def __init__(self, applies=(), broken=()):
        self.applies = applies      # ходы, на которых панель применяет список; или функция от хода
        self.broken = set(broken)   # виды проб, которые на самом деле не работают
        self.tick = 0
        self.stamp = 0
        self.granted = set()
        self.session = None

    def step(self):
        self.tick += 1
        if self.tick > 5000:
            raise AssertionError('проверка не кончается')
        due = self.applies(self.tick) if callable(self.applies) else self.tick in self.applies
        if due:
            self.stamp += 1
            self.granted.clear()
            self.session = None


class Tunnel:
    """Протокол вроде IKEv2: доступ в файле проверки применение переживает, сессия - нет."""
    name = 'Туннель'
    ports = [('udp', 1194)]

    def __init__(self, world):
        self.world = world
        self.peers = {}

    def issue(self, identity, _address):
        self.peers[identity] = {'revoked': False}
        self.grant(identity)

    def grant(self, identity):
        if not self.peers[identity]['revoked']:
            self.world.granted.add(identity)

    def allowed(self, identity):
        return not self.peers[identity]['revoked']

    def connect(self, identity, _endpoint):
        self.world.step()
        self.world.session = identity if self.allowed(identity) else None
        return self.world.session == identity

    def down(self):
        self.world.session = None

    def revoke(self, identity):
        self.peers[identity]['revoked'] = True
        self.world.granted.discard(identity)
        if self.world.session == identity:
            self.world.session = None

    def lost(self, identity, linked):
        return linked and self.world.session != identity


class Peers(Tunnel):
    """Протокол вроде WireGuard: применение панели снимает и самого пира проверки."""
    name = 'Пиры'

    def allowed(self, identity):
        return identity in self.world.granted

    def ensure(self, identity):
        self.grant(identity)

    def lost(self, identity, _linked):
        return identity not in self.world.granted

    @staticmethod
    def free_addresses(count):
        return [f'10.8.0.{250 - i}' for i in range(count)]


def answer(kind, args):
    if kind == 'dns':
        return ['192.0.2.53'], 'dns-unfiltered'
    if kind == 'echo' and args[2] != 'm.se':
        return {'peer': SOURCE, 'local': True}, vt.DIRECT
    return {'peer': SOURCE, 'local': True}, 'hy2-se-direct'


def check(world, kind=Peers):
    """Полная проверка одного протокола на модели: {проверка: результат}, код отчёта."""
    report = vt.Report('ru')
    verifier = vt.Verifier.__new__(vt.Verifier)
    verifier.report, verifier.router, verifier.exits = report, None, ['se']
    verifier.markers = {vt.DIRECT: 'm.direct', 'se': 'm.se'}
    verifier.targets = types.SimpleNamespace(port=8080, echo=object(), remote={'se': object()}, failed={})
    verifier.internet = [('192.0.2.10', 'example.test', '/', 204)]
    verifier.dns_reference = {'10.99.0.1': ['192.0.2.53'], '127.0.0.2': ['192.0.2.53']}
    verifier.relay, verifier.relay_ok = types.SimpleNamespace(start=lambda _proto, _port: 40000), True

    def in_namespace(_router, _source, probe, args, _dest=None):
        world.step()
        if world.session is None or probe in world.broken:
            return None, []
        result, chain = answer(probe, args)
        return result, [{'chains': [chain], 'rule': 'rule_set', 'payload': 'manual-pin-' + chain}]

    with mock.patch.object(vt, 'in_namespace', in_namespace), \
            mock.patch.object(vt, 'applied', lambda: world.stamp, create=True), \
            mock.patch.object(vt, 'probe_internet', lambda *_args: True), \
            mock.patch.object(vt.Namespace, 'source', staticmethod(lambda: SOURCE)), \
            mock.patch.object(vt.time, 'sleep', lambda _seconds: None), redirect_stdout(io.StringIO()):
        verifier.protocol(kind(world))
    world.reasons = [row['reason'] for row in report.rows]
    return {row['check']: row['result'] for row in report.rows}, report.code()


GREEN = (dict.fromkeys(CHECKS, OK), 0)


class ClientListMovesTests(unittest.TestCase):
    def test_quiet_panel(self):
        for kind in (Peers, Tunnel):
            with self.subTest(kind=kind.name):
                self.assertEqual(check(World(), kind), GREEN)

    def test_device_issued_at_any_moment(self):
        for kind in (Peers, Tunnel):
            quiet = World()
            check(quiet, kind)
            for moment in range(1, quiet.tick + 1):
                with self.subTest(kind=kind.name, moment=moment):
                    self.assertEqual(check(World({moment}), kind), GREEN)

    def test_device_issued_then_deleted(self):
        for kind in (Peers, Tunnel):
            quiet = World()
            check(quiet, kind)
            for moment in range(1, quiet.tick + 1):
                for later in (1, 2, 3):
                    with self.subTest(kind=kind.name, moment=moment, later=later):
                        self.assertEqual(check(World({moment, moment + later}), kind), GREEN)

    def test_twenty_devices_churned_over_five_runs(self):
        for kind in (Peers, Tunnel):
            world = World(lambda tick: tick % 3 == 0)
            for run in range(5):
                with self.subTest(kind=kind.name, run=run):
                    self.assertEqual(check(world, kind), GREEN)
            self.assertGreaterEqual(world.stamp, 40)

    def test_broken_dns_is_caught_under_churn(self):
        for applies in ((), lambda tick: tick % 3 == 0):
            for kind in (Peers, Tunnel):
                with self.subTest(kind=kind.name, churn=bool(applies)):
                    rows, code = check(World(applies, broken={'dns'}), kind)
                    self.assertEqual((rows['DNS AdGuard'], rows['DNS внешний'], code), (FAIL, FAIL, 1))
                    self.assertEqual(rows['маршрут: прямой'], OK)

    def test_dead_tunnel_is_caught_under_churn(self):
        for applies in ((), lambda tick: tick % 3 == 0):
            for kind in (Peers, Tunnel):
                with self.subTest(kind=kind.name, churn=bool(applies)):
                    rows, code = check(World(applies, broken={'echo', 'dns', 'egress', 'internet'}), kind)
                    self.assertEqual((rows['маршрут: прямой'], rows['интернет через экзит'], code), (FAIL, FAIL, 1))

    def test_panel_applying_at_every_step_is_not_measured(self):
        for kind in (Peers, Tunnel):
            with self.subTest(kind=kind.name):
                world = World(lambda _tick: True)
                rows, code = check(world, kind)
                self.assertEqual((code, FAIL in rows.values()), (2, False))
                self.assertIn('панель', ' '.join(world.reasons))


class Control:
    """Служба управления с пирами wg0; панель успевает применить свой список `races` раз."""

    def __init__(self, races=0, error='conflict'):
        self.races, self.error = races, error
        self.live = ['panel-1']
        self.calls = 0

    def call(self, _method, params):
        self.calls += 1
        if self.races:
            self.races -= 1
            if self.error in ('stale', 'conflict'):
                self.live.append(f'panel-{len(self.live) + 1}')  # устройство, выпущенное в панели
            raise vt.ControlError(self.error)
        self.live = [peer['key'] for peer in params['peers']]

    def run(self, *args, **_kwargs):
        if args[:4] == ('wg', 'show', 'wg0', 'peers'):
            return types.SimpleNamespace(stdout=' '.join(self.live))
        return types.SimpleNamespace(stdout={'genkey': 'private', 'pubkey': 'verify', 'genpsk': 'psk'}[args[1]])

    def peers(self, _text):
        return [{'key': key, 'preshared_key': 'psk', 'ip': '10.66.66.2'} for key in self.live]


def apply_check(control):
    """wg_apply_check на модели службы: строки отчёта, код, сколько раз возвращено сохранённое."""
    report = vt.Report('ru')
    restored = []

    def restore():
        # как настоящий: без сохранённой копии возвращать нечего
        if (vt.WORK / 'wg0.conf').exists():
            restored.append(vt.forget_wg_apply())
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / 'conf').write_bytes(b'')
        with mock.patch.multiple(vt, WORK=work / 'work', WG_CONF=work / 'conf', CONTROL_SOCKET=work,
                                 CONTROL_WG_STATE=work / 'absent.json', run=control.run,
                                 control_call=control.call, apply_peers=control.peers,
                                 restore_wg_apply=restore), \
                mock.patch.object(vt.WireGuard, 'free_addresses', staticmethod(lambda _count: ['10.66.66.250'])), \
                mock.patch.object(vt.time, 'sleep', lambda _seconds: None), redirect_stdout(io.StringIO()):
            vt.WORK.mkdir()
            vt.wg_apply_check(report)
            left = sorted(item.name for item in vt.WORK.iterdir())
    return [(row['result'], row['reason']) for row in report.rows], report.code(), len(restored), left


class PanelAppliesDuringApplyCheckTests(unittest.TestCase):
    def test_quiet_panel(self):
        control = Control()
        rows, code, restored, _left = apply_check(control)
        self.assertEqual(([result for result, _ in rows], code, restored, control.live), ([OK], 0, 1, ['panel-1']))

    def test_panel_applied_first_is_not_rolled_back(self):
        # Панель применила своё между снимком проверки и её wg.apply: служба ответила
        # conflict и ничего не меняла. Возврат сохранённого снял бы устройство панели.
        for error in ('conflict', 'stale'):
            with self.subTest(error=error):
                control = Control(races=1, error=error)
                rows, code, restored, _left = apply_check(control)
                self.assertEqual(([result for result, _ in rows], code, restored), ([OK], 0, 1))
                self.assertEqual(control.live, ['panel-1', 'panel-2'])

    def test_panel_applying_every_time_is_not_measured(self):
        control = Control(races=99)
        rows, code, restored, left = apply_check(control)
        self.assertEqual(([result for result, _ in rows], code, restored, left), ([UNKNOWN], 2, 0, []))
        self.assertIn('панель', rows[0][1])
        self.assertLess(control.calls, 10)

    def test_refused_apply_stays_an_error(self):
        rows, code, restored, _left = apply_check(Control(races=1, error='unavailable'))
        self.assertEqual(([result for result, _ in rows], code, restored), ([FAIL], 1, 1))


if __name__ == '__main__':
    unittest.main()
