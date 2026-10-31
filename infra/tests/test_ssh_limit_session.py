"""Предел ssh гасит всю сессию команды и не подменяет причину отказа.

_limit_run запускает команду в своей сессии. По пределу гибнет всё, что в
сессии осталось, и сирота с родителем 1 тоже: подставной ssh отдаёт ребёнка
через `( ... & )` или nohup и висит. Причина ssh_probe - диагностика ssh, а не
сообщение bash «Killed» о задании. Предел держится и у лидера группы (`set -m`:
setsid без -w уходит в фон, вызов возвращает 0 сразу). Много детей не тянут
возврат дольше предела с запасом. Неверный SSH_PROBE_TIMEOUT pre-flight
называет настройкой, а не «SSH недоступен» на каждом экзите.
_limit_run в infra/lib/ssh.sh и в 99-verify (на узел lib/ssh.sh не попадает)
- одна реализация: края проверяются на обеих, тексты функций совпадают.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import test_verify
from test_deploy import ROOT, source
from test_ssh_limit_remote_code import Node, alive
from test_ssh_probe_timeout_value import FAST_SLEEP
from test_ssh_refusal import PREFLIGHT
from test_verify_exit_ssh_bashrc import LABEL, MOCK

LIMIT = 2
HANG = 30
# Запас на добивание не меньше 2 с: под нагрузкой меньший запас падал.
BOUND = LIMIT + 4
MANY = 200
STUB_REASON = 'stub-ssh: kex_exchange_identification: read stalled'
KILLED = r'Killed|line \d+'
# Подставной sshd: разбор аргументов как у ssh, затем режим из FAKE_BASHRC.
# Каждый процесс заглушки пишет свой pid в $FAKE_NODE/pids.
FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
echo $$ >> "$FAKE_NODE/calls"
echo $$ >> "$FAKE_NODE/pids"
held() { bash -c 'echo $$ >> "$FAKE_NODE/pids"; exec sleep "$FAKE_SLEEP"'; }
case ${FAKE_BASHRC:-} in
  # Родитель ребёнка выходит сразу: сирота с родителем 1 в сессии предела.
  orphan) ( held </dev/null >/dev/null 2>&1 & ) ;;
  nohup)  ( nohup bash -c 'echo $$ >> "$FAKE_NODE/pids"; exec sleep "$FAKE_SLEEP"' </dev/null >/dev/null 2>&1 & ) ;;
  hang)   ;;
  says)   echo "$FAKE_REASON" >&2 ;;
  many)   for ((i = 0; i < FAKE_MANY; i++)); do sleep "$FAKE_SLEEP" </dev/null >/dev/null 2>&1 &
            echo $! >> "$FAKE_NODE/pids"; done ;;
  *)      bash -c "$*"; exit ;;
esac
exec sleep "$FAKE_SLEEP"
'''


def preamble(side):
    """Текст bash, после которого _limit_run определён так, как его видит сторона.

    lib - infra/lib/ssh.sh на entry. node - 99-verify на узле: свои функции
    _limit* из скрипта, а если их там нет, то из infra/lib/anysda-svc.sh, что
    кладётся на каждый узел.
    """
    if side == 'lib':
        return f'DEPLOY_ROOT={ROOT}/infra\nsource {ROOT}/infra/lib/ssh.sh\n'
    verify = source('infra/scripts/99-verify.sh')
    names = re.findall(r'^(_limit\w*)\(\) \{', verify, re.M)
    if not names:
        assert 'anysda-svc.sh' in verify, '99-verify не определяет _limit_run и не берёт его из anysda-svc.sh'
        return f'source {ROOT}/infra/lib/anysda-svc.sh\n'
    return ''.join(f'{n}() {{' + verify.split(f'\n{n}() {{', 1)[1].split('\n}', 1)[0] + '\n}\n' for n in names)


class StubNode(Node):
    def __init__(self, mode, **env):
        super().__init__(mode, HANG)
        (self.root / 'bin' / 'ssh').write_text(FAKE_SSH)
        self.env.update({'FAKE_REASON': STUB_REASON, 'FAKE_MANY': str(MANY), **env})

    def direct(self, side, body):
        """(код, stdout, stderr, секунды) сценария с _limit_run стороны side."""
        started = time.monotonic()
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', preamble(side) + body], cwd=self.root,
                                env=self.env, text=True, capture_output=True, timeout=60)
        return result.returncode, result.stdout, result.stderr, time.monotonic() - started


def limited(leader=False):
    """Сценарий: _limit_run над подставным ssh, код в stdout."""
    return ('set -m\n' if leader else '') + f'rc=0; _limit_run {LIMIT} ssh node.example true || rc=$?\necho "rc=$rc"\n'


class SshLimitSessionTests(unittest.TestCase):
    def assert_nothing_left(self, what, pids, elapsed, bound=BOUND, least=2):
        self.assertGreaterEqual(len(pids), least, f'{what}: заглушка ssh не записала pid свой и детей')
        self.assertLess(elapsed, bound, f'{what}: {elapsed:.1f} с при пределе {LIMIT} с')
        deadline = time.monotonic() + 2
        while alive(pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        live = alive(pids)
        names = []
        for pid in live:
            try:
                names.append(Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode().strip())
            except OSError:
                pass
        self.assertEqual(live, [], f'{what}: после предела живы процессы заглушки ({len(live)}): {names[:5]}')

    def test_1_orphan_in_session_dies_lib(self):
        for mode in ('orphan', 'nohup'):
            for call in (Node.probe, Node.fresh):
                with self.subTest(mode=mode, call=call.__name__), StubNode(mode) as node:
                    code, out, err, elapsed = call(node, str(LIMIT))
                    print(f'{mode} {call.__name__}: rc={code} {elapsed:.1f}s {out.strip()!r}')
                    self.assertNotEqual(code, 0, err)
                    self.assert_nothing_left(f'{mode} {call.__name__}', node.pids(), elapsed)
            for side in ('lib', 'node'):
                with self.subTest(mode=mode, call=f'_limit_run {side}'), StubNode(mode) as node:
                    code, out, err, elapsed = node.direct(side, limited())
                    self.assertRegex(out, r'rc=(124|137)\b', err)
                    self.assert_nothing_left(f'{mode} _limit_run {side}', node.pids(), elapsed)

    def verify(self, directory, bashrc):
        root = Path(directory)
        (root / 'node').mkdir(exist_ok=True)
        fake = root / 'ssh'
        fake.write_text(FAKE_SSH)
        fake.chmod(0o755)
        env = {'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc, 'FAKE_SLEEP': str(HANG),
               'FAKE_NODE': str(root / 'node'), 'SSH_PROBE_TIMEOUT': str(LIMIT)}
        started = time.monotonic()
        with mock.patch.object(test_verify, 'MOCK', MOCK):
            result = test_verify.VerifyTest('run_verify').run_verify(extra_env=env)
        elapsed = time.monotonic() - started
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        return rows[0], elapsed

    def test_1_orphan_in_session_dies_exit_ssh(self):
        for mode in ('orphan', 'nohup'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='ssh-limit-session-') as directory:
                pids = Path(directory) / 'node' / 'pids'
                try:
                    _, baseline = self.verify(directory, '')
                    pids.unlink(missing_ok=True)
                    row, elapsed = self.verify(directory, mode)
                    print(f'exit_ssh {mode}: {elapsed:.1f}s {row["result"]}: {row["reason"]}')
                    self.assertEqual(row['result'], 'Ошибка')
                    self.assertRegex(row['reason'], rf'предел {LIMIT} с')
                    self.assert_nothing_left(f'exit_ssh {mode}', [int(p) for p in pids.read_text().split()],
                                             elapsed - baseline)
                finally:
                    if pids.exists():
                        for pid in alive(int(p) for p in pids.read_text().split()):
                            os.kill(pid, 9)

    def test_2_reason_is_ssh_not_killed(self):
        with StubNode('says') as node:
            code, out, err, elapsed = node.probe(str(LIMIT))
            print(f'ssh_probe says: rc={code} {elapsed:.1f}s {out.strip()!r}')
            self.assertEqual(code, 1, out + err)
            self.assertRegex(out, rf'нет ответа за {LIMIT} с')
            self.assertIn(STUB_REASON, out, 'причина ssh_probe - не диагностика ssh')
            self.assertNotRegex(out + err, KILLED, 'в причину ssh_probe попало сообщение bash о задании')
        with StubNode('says') as node:
            code, out, err, _ = node.fresh(str(LIMIT))
            self.assertIn(code, (124, 137), err)
            self.assertIn(STUB_REASON, err)
            self.assertNotRegex(err, KILLED, 'ssh_fresh пишет вызывающему сообщение bash о задании')
        for side in ('lib', 'node'):
            with self.subTest(side=side), StubNode('says') as node:
                code, out, err, _ = node.direct(side, limited())
                self.assertRegex(out, r'rc=(124|137)\b', err)
                self.assertEqual(err.strip(), STUB_REASON, f'_limit_run {side}: stderr не только команды')

    def test_3_limit_holds_for_group_leader(self):
        for side in ('lib', 'node'):
            with self.subTest(call=f'_limit_run {side}'), StubNode('hang') as node:
                code, out, err, elapsed = node.direct(side, limited(leader=True))
                print(f'set -m _limit_run {side}: {out.strip()!r} {elapsed:.2f}s')
                self.assertRegex(out, r'rc=(124|137)\b', f'код не предела при set -m: {out + err}')
                self.assertGreater(elapsed, LIMIT - 0.5, 'вернулся раньше предела: команда живёт без предела')
                self.assert_nothing_left(f'set -m _limit_run {side}', node.pids(), elapsed, least=1)
        with self.subTest(call='ssh_probe'), StubNode('hang') as node:
            code, out, err, elapsed = node.run('set -m\nssh_probe\n', str(LIMIT))
            self.assertEqual(code, 1, out + err)
            self.assertRegex(out, rf'нет ответа за {LIMIT} с')
            self.assertGreater(elapsed, LIMIT - 0.5)
            self.assert_nothing_left('set -m ssh_probe', node.pids(), elapsed, least=1)

    def test_4_many_children(self):
        for side in ('lib', 'node'):
            with self.subTest(side=side), StubNode('many') as node:
                code, out, err, elapsed = node.direct(side, limited())
                print(f'{MANY} детей _limit_run {side}: {out.strip()!r} {elapsed:.1f}s')
                self.assertRegex(out, r'rc=(124|137)\b', err)
                self.assertGreater(len(node.pids()), MANY, 'заглушка не породила детей')
                self.assert_nothing_left(f'{MANY} детей {side}', node.pids(), elapsed, LIMIT + 10)

    def test_5_preflight_bad_limit_is_setting_not_unreachable(self):
        body = FAST_SLEEP + PREFLIGHT.replace('expand_hosts() { echo ru; }', 'expand_hosts() { echo ru de nl; }')
        self.assertNotEqual(body, FAST_SLEEP + PREFLIGHT)
        with StubNode('') as node:
            code, out, err, _ = node.run(body, 'abc')
            out += err
            print(f'pre-flight abc: rc={code} {out.strip().splitlines()[-3:]!r}')
            self.assertNotEqual(code, 0, out)
            self.assertIn('SSH_PROBE_TIMEOUT', out, out)
            self.assertNotIn('SSH недоступен', out, 'неверная настройка названа недоступностью экзита')
            self.assertEqual(node.calls(), 0)

    def test_6_one_limit_run_for_entry_and_node(self):
        texts = {}
        for side in ('lib', 'node'):
            script = (preamble(side) + 'declare -F | awk \'$3 ~ /^_limit/ {print $3}\' | sort | '
                      'while read -r f; do declare -f "$f"; done\n')
            result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            texts[side] = result.stdout
        self.assertIn('_limit_run ()', texts['lib'])
        self.assertEqual(texts['node'], texts['lib'], '_limit_run в lib/ssh.sh и в 99-verify разъехались')


if __name__ == '__main__':
    unittest.main()
