"""Предел ssh гасит всю сессию команды и не подменяет причину отказа.

_limit_run запускает команду в своей сессии. По пределу гибнет всё, что в
сессии осталось, и сирота с родителем 1 тоже: подставной ssh отдаёт ребёнка
через `( ... & )` или nohup и висит. Причина ssh_probe - диагностика ssh, а не
сообщение bash «Killed» о задании. Предел держится и у лидера группы (`set -m`:
setsid без -w уходит в фон, вызов возвращает 0 сразу). Много детей не тянут
возврат дольше предела с запасом, и 2000 детей и зомби тоже. Неверный
SSH_PROBE_TIMEOUT pre-flight называет настройкой, а не «SSH недоступен» на
каждом экзите, и один раз на все узлы; текст называет условие, которое
значение нарушило. Уборка сессии после wait не бьёт чужую сессию, что успела
получить освободившийся номер (свой pid-namespace под sudo -n). Ctrl-C и TERM
вызывающего гасят команду сразу, а не по пределу. Сирота экзита, ответившего
сразу, гибнет и через exit_ssh: его гасит сам _limit_run, не убийца.
_limit_run в infra/lib/ssh.sh и в 99-verify (на узел lib/ssh.sh не попадает)
- одна реализация: края проверяются на обеих, тексты функций совпадают.
"""
import os
from pathlib import Path
import re
import shutil
import signal
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
  # Половина - зомби: python их не пожинает, половина - спящие дети.
  swarm)  python3 -c 'import os, sys, time
for _ in range(int(sys.argv[1])):
    if os.fork() == 0:
        os._exit(0)
time.sleep(float(sys.argv[2]))' "$((FAKE_MANY / 2))" "$FAKE_SLEEP" </dev/null >/dev/null 2>&1 &
          echo $! >> "$FAKE_NODE/pids"
          for ((i = 0; i < FAKE_MANY / 2; i++)); do sleep "$FAKE_SLEEP" </dev/null >/dev/null 2>&1 &
            echo $! >> "$FAKE_NODE/pids"; done ;;
  # Сирота в сессии, а заглушка отвечает сразу: команда кончилась до предела.
  early-*) n=$(wc -l < "$FAKE_NODE/pids")
           case ${FAKE_BASHRC#early-} in
             orphan) ( held </dev/null >/dev/null 2>&1 & ) ;;
             nohup)  ( nohup bash -c 'echo $$ >> "$FAKE_NODE/pids"; exec sleep "$FAKE_SLEEP"' </dev/null >/dev/null 2>&1 & ) ;;
           esac
           while (( $(wc -l < "$FAKE_NODE/pids") <= n )); do sleep 0.05; done
           bash -c "$*"; exit ;;
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


def limited(leader=False, limit=LIMIT):
    """Сценарий: _limit_run над подставным ssh, код в stdout."""
    return ('set -m\n' if leader else '') + f'rc=0; _limit_run {limit} ssh node.example true || rc=$?\necho "rc=$rc"\n'


# Свой pid-namespace: номер, что освободил wait, щуп сразу отдаёт новой сессии
# (ns_last_pid), и та живёт вне сессии команды. $1 - файл с _limit_run,
# $2 - секунд команды, $3 - предел, $4 - повторов, $5 - секунд охоты за номером,
# $6 - каталог. Строка на повтор: alive - жертва жива, KILLED - убита,
# free - номер не освободился.
FOREIGN = r'''source "$1"
d=$6
for ((i = 0; i < $4; i++)); do
  rm -f "$d/target" "$d/victim"
  ( while [[ ! -s $d/target ]]; do :; done
    t=$(<"$d/target"); end=$((SECONDS + $5))
    while (( SECONDS < end )); do
      echo $((t - 1)) > /proc/sys/kernel/ns_last_pid
      bash -c 'exec setsid -w sleep 30' </dev/null >/dev/null 2>&1 & v=$!
      if (( v == t )); then echo "$v" > "$d/victim"; break; fi
      kill "$v" 2>/dev/null
    done ) & spin=$!
  rc=0; _limit_run "$3" bash -c 'echo $$ > "$1"; exec sleep "$2"' _ "$d/target" "$2" || rc=$?
  wait "$spin"
  sleep 0.3
  if [[ ! -s $d/victim ]]; then echo "rc=$rc free"; continue; fi
  v=$(<"$d/victim")
  if [[ $(ps -o stat= -p "$v") == [^Z]* ]]; then echo "rc=$rc alive sid=$(ps -o sid= -p "$v")"; kill "$v"
  else echo "rc=$rc KILLED"; fi
done
'''
# Без уборки сессии: щуп обязан занять освободившийся номер, иначе тест пуст.
NO_CLEANUP = '_limit_run() { local pid rc=0; shift; set +m; setsid -w "$@" <&0 & pid=$!; wait "$pid" || rc=$?; return $rc; }\n'
# Признаки условия в тексте ошибки SSH_PROBE_TIMEOUT, а не точная фраза.
CONDITION = {'целое': r'цел',
             'от 1': r'от 1\b|больше (0|нуля)|не меньше 1|≥ ?1|>= ?1|положительн',
             'до 999999': r'999 ?999|шест\w* цифр|6 цифр|меньше (1 ?000 ?000|миллиона)',
             'без ведущего нуля': r'ведущ\w* (0|нул)|не (начина\w* )?с (0|нуля)|без (0|нуля) в начале'}
SIGNAL_GRACE = 2


def lingering(pids):
    """Живые и не в выходе: под нагрузкой убитый родитель тысячи зомби выходит дольше 2 с (PF_EXITING)."""
    live = []
    for pid in alive(pids):
        try:
            flags = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[6])
        except (FileNotFoundError, ProcessLookupError):
            continue
        if not flags & 0x4:
            live.append(pid)
    return live


def sudo_unshare():
    """Причина пропуска, если своего pid-namespace под sudo -n нет."""
    if not shutil.which('unshare'):
        return 'нет unshare'
    probe = subprocess.run(['sudo', '-n', 'unshare', '-pf', '--mount-proc', 'true'], capture_output=True, timeout=30)
    return None if probe.returncode == 0 else 'нет sudo -n unshare: ' + probe.stderr.decode(errors='replace').strip()


def default_signals():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


class SshLimitSessionTests(unittest.TestCase):
    def assert_nothing_left(self, what, pids, elapsed, bound=BOUND, least=2):
        self.assertGreaterEqual(len(pids), least, f'{what}: заглушка ssh не записала pid свой и детей')
        self.assertLess(elapsed, bound, f'{what}: {elapsed:.1f} с при пределе {LIMIT} с')
        deadline = time.monotonic() + 2
        while lingering(pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        live = lingering(pids)
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

    def verify(self, directory, bashrc, limit=str(LIMIT)):
        root = Path(directory)
        (root / 'node').mkdir(exist_ok=True)
        fake = root / 'ssh'
        fake.write_text(FAKE_SSH)
        fake.chmod(0o755)
        env = {'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc, 'FAKE_SLEEP': str(HANG),
               'FAKE_NODE': str(root / 'node'), 'SSH_PROBE_TIMEOUT': limit}
        started = time.monotonic()
        with mock.patch.object(test_verify, 'MOCK', MOCK):
            result = test_verify.VerifyTest('run_verify').run_verify(extra_env=env)
        elapsed = time.monotonic() - started
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        return rows[0], elapsed

    def test_1_orphan_in_session_dies_exit_ssh(self):
        # early-*: экзит ответил сразу, сирота в сессии ssh остался. Его гасит
        # сам _limit_run после wait, а не убийца по пределу: убийцу к этому
        # времени уже сняли.
        for mode in ('orphan', 'nohup', 'early-orphan', 'early-nohup'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='ssh-limit-session-') as directory:
                pids = Path(directory) / 'node' / 'pids'
                try:
                    _, baseline = self.verify(directory, '')
                    pids.write_text('')
                    row, elapsed = self.verify(directory, mode)
                    print(f'exit_ssh {mode}: {elapsed:.1f}s {row["result"]}: {row["reason"]}')
                    if mode.startswith('early-'):
                        self.assertEqual(row['result'], 'Работает', row['reason'])
                    else:
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

    def foreign(self, limit_text, seconds, limit, times, hunt):
        with tempfile.TemporaryDirectory(prefix='ssh-limit-foreign-') as directory:
            script, lib = Path(directory, 'foreign.sh'), Path(directory, 'limit.sh')
            script.write_text(FOREIGN)
            lib.write_text(limit_text)
            result = subprocess.run(['sudo', '-n', 'unshare', '-pf', '--mount-proc', 'bash', str(script), str(lib),
                                     str(seconds), str(limit), str(times), str(hunt), directory],
                                    text=True, capture_output=True, timeout=120)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), times, f'щуп не прошёл все повторы: {result.stdout}{result.stderr}')
        return lines

    def test_7_freed_session_number_is_not_killed(self):
        reason = sudo_unshare()
        if reason:
            self.skipTest(reason)
        control = self.foreign(NO_CLEANUP, 0.3, HANG, 3, 2)
        print(f'без уборки: {control}')
        self.assertTrue([line for line in control if ' alive' in line],
                        f'щуп не занял освободившийся номер, тест пуст: {control}')
        for side in ('lib', 'node'):
            for case, seconds, limit, times, hunt in (('ранний конец', 0.3, HANG, 5, 2), ('предел', HANG, 1, 3, 3)):
                with self.subTest(side=side, case=case):
                    lines = self.foreign(preamble(side), seconds, limit, times, hunt)
                    print(f'чужая сессия {side} {case}: {lines}')
                    self.assertEqual([line for line in lines if 'KILLED' in line], [],
                                     f'_limit_run {side}, {case}: убита чужая сессия с номером команды: {lines}')

    def interrupt(self, script, env, cwd, pids, how):
        """(код вызывающего, живые pid заглушки, осталось ли что в его группе, секунд после сигнала)."""
        sig = signal.SIGINT if how == 'INT' else signal.SIGTERM
        caller = subprocess.Popen(['bash', '-euo', 'pipefail', '-c', script], cwd=cwd, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                  preexec_fn=default_signals)
        try:
            deadline = time.monotonic() + 10
            while len(pids()) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(len(pids()), 2, 'заглушка ssh не записала pid свой и сироты')
            sent = time.monotonic()
            # INT - группе, как Ctrl-C в терминале; TERM - одному вызывающему.
            (os.killpg if how == 'INT' else os.kill)(caller.pid, sig)
            deadline = sent + SIGNAL_GRACE + 2
            try:
                caller.wait(timeout=SIGNAL_GRACE + 2)
            except subprocess.TimeoutExpired:
                pass
            while alive(pids()) and time.monotonic() < deadline:
                time.sleep(0.1)
            gone = time.monotonic() - sent
            try:
                os.killpg(caller.pid, 0)
                left = True
            except ProcessLookupError:
                left = False
            return caller.poll(), alive(pids()), left, gone
        finally:
            try:
                os.killpg(caller.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            caller.communicate()

    def assert_interrupted(self, what, how, result):
        code, live, left, gone = result
        sig = signal.SIGINT if how == 'INT' else signal.SIGTERM
        print(f'{what} {how}: rc={code} живы={live} группа={left} {gone:.1f}s')
        self.assertIn(code, (-sig, 128 + sig), f'{what}: вызывающий не вышел по {how}')
        self.assertEqual(live, [], f'{what}: заглушка жива через {gone:.1f} с после {how}, гаснет только по пределу')
        self.assertFalse(left, f'{what}: после {how} в группе вызывающего остались процессы (убийца по пределу)')

    def test_8_int_term_kill_command_now(self):
        for side in ('lib', 'node'):
            for how in ('INT', 'TERM'):
                with self.subTest(side=side, how=how), StubNode('orphan') as node:
                    script = preamble(side) + f'_limit_run {HANG} ssh node.example true\necho after\n'
                    self.assert_interrupted(f'_limit_run {side}', how,
                                            self.interrupt(script, node.env, node.root, node.pids, how))
        # Ctrl-C во время ssh_fresh в $(...), как в стадиях deploy.
        with self.subTest(side='ssh_fresh', how='INT'), StubNode('orphan') as node:
            script = (f'DEPLOY_ROOT={ROOT}/infra\nsource {ROOT}/infra/lib/ssh.sh\n'
                      'SSH_USER=root SSH_HOST=node.example SSH_PASS=fixture\n'
                      'out=$(ssh_fresh echo ok 2>&1) || echo "rc=$?"\n')
            self.assert_interrupted('ssh_fresh', 'INT', self.interrupt(
                script, {**node.env, 'SSH_PROBE_TIMEOUT': str(HANG)}, node.root, node.pids, 'INT'))

    def assert_condition(self, what, value, code, message):
        print(f'{what} {value!r}: rc={code} {message!r}')
        self.assertNotEqual(code, 0, f'{what}: SSH_PROBE_TIMEOUT={value!r} принят')
        self.assertIn('SSH_PROBE_TIMEOUT', message, f'{what}: в ошибке нет имени переменной')
        for name, pattern in CONDITION.items():
            self.assertRegex(message, pattern, f'{what}: SSH_PROBE_TIMEOUT={value!r}: в условии нет «{name}»')

    def test_9_bad_limit_message_names_condition(self):
        for value in ('1000000', '08', '0', 'abc'):
            with self.subTest(side='ssh_probe_limit', value=value), StubNode('') as node:
                code, out, err, _ = node.run('ssh_probe_limit\n', value)
                self.assert_condition('ssh_probe_limit', value, code, (out + err).strip())
            with self.subTest(side='exit_ssh', value=value), \
                    tempfile.TemporaryDirectory(prefix='ssh-limit-session-') as directory:
                row, _ = self.verify(directory, '', value)
                self.assert_condition('exit_ssh', value, row['result'] != 'Работает', row['reason'])
                self.assertFalse(Path(directory, 'node', 'calls').exists(), 'exit_ssh запустил ssh')

    def test_10_preflight_bad_limit_said_once(self):
        body = FAST_SLEEP + PREFLIGHT.replace('expand_hosts() { echo ru; }', 'expand_hosts() { echo ru de nl; }')
        with StubNode('') as node:
            code, out, err, _ = node.run(body, 'abc')
            out += err
            said = [line for line in out.splitlines() if re.search(r'\babc\b', line)]
            print(f'pre-flight abc на 3 узлах: rc={code} {said!r}')
            self.assertNotEqual(code, 0, out)
            self.assertEqual(len(said), 1, f'причина неверной настройки не один раз:\n{out}')
            self.assertEqual(node.calls(), 0)

    def test_11_swarm_returns_in_time(self):
        limit, many = 5, 2000
        for side in ('lib', 'node'):
            with self.subTest(side=side), StubNode('swarm', FAKE_MANY=str(many)) as node:
                code, out, err, elapsed = node.direct(side, limited(limit=limit))
                print(f'{many} детей и зомби _limit_run {side}: {out.strip()!r} {elapsed:.1f}s')
                self.assertRegex(out, r'rc=(124|137)\b', err)
                self.assertGreater(len(node.pids()), many // 2, 'заглушка не породила детей')
                self.assertLess(elapsed, limit + 10, f'{side}: возврат через {elapsed:.1f} с при пределе {limit} с')
                self.assert_nothing_left(f'{many} детей {side}', node.pids(), elapsed, limit + 10)


if __name__ == '__main__':
    unittest.main()
