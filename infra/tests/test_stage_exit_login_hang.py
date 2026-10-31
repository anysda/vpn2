"""Зависший вход на экзит не держит стадию: предел на вход до метки начала.

Настоящие run_stage и run_stage_on_host из infra/deploy.sh с настоящим
infra/lib/ssh.sh (push, ssh_exec, ssh_fresh). sshpass и ssh подменены:
подставной ssh ведёт себя как login shell узла - сначала .bashrc (зависание,
шум или exit по режиму), потом команда через bash, а пути узла (/tmp/anysda,
/run/vpn2-fw-guard, apt) переписаны в каталог узла во временной папке.
Зависший .bashrc экзита nl должен дать строку с именем экзита и «завис» за
предел SSH_PROBE_TIMEOUT, а не держать 99-verify, пока .bashrc не отпустит.
"""
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest

from test_deploy import source

FAKE_SSHPASS = '#!/bin/bash\n[[ $1 == -e ]] && shift\nexec "$@"\n'
FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) dest=$1; shift; break ;; esac
done
tag=${dest#*@}; tag=${tag%%.*}
node=$FAKE_NODES/$tag
mkdir -p "$node"
cmd="$*"
for path in /tmp/anysda /run/vpn2-fw-guard /etc/apt/sources.list.d /usr/share/keyrings; do
  cmd=${cmd//$path/$node}
done
echo $$ >> "$FAKE_PIDS"
hang=0
if [[ $tag == "${FAKE_HANG_TAG:-}" ]]; then
  case $FAKE_HANG_ON in
    all) hang=1 ;;
    push) [[ $cmd == *'cat > '* || $cmd == *'tar -xmf'* ]] && hang=1 ;;
    script) [[ $cmd == *'chmod +x '* ]] && hang=1 ;;
  esac
fi
if (( hang )); then
  echo "$tag" >> "$FAKE_HANGS"
  sleep "$FAKE_SLEEP" & echo $! >> "$FAKE_PIDS"; wait $!
fi
if [[ $tag == "${FAKE_BASHRC_TAG:-}" ]]; then
  case $FAKE_BASHRC in
    noise) printf 'Welcome to exit node\nmotd line\n' ;;
    exit0) exit 0 ;;
  esac
fi
exec bash -c "$cmd"
'''
STAGE = r'''#!/bin/bash
set -euo pipefail
source "$1"
if [[ $HOST_TAG == "${FAKE_SLOW_TAG:-}" ]]; then
  for i in $(seq "$FAKE_SLOW"); do echo "[$HOST_TAG] установка, шаг $i"; sleep 1; done
fi
echo "[$HOST_TAG] ✓ стадия $(basename "$0" .sh) пройдена"
'''
LIMIT = 3
HANG = 60
EXITS = ('nl', 'de')
NOISE = ('Welcome to exit node', 'motd line')
HUNG = re.compile(r'\bnl\b.*завис|завис.*\bnl\b')
STUBS = ('lib/anysda-pki.sh', 'lib/release.sh', 'lib/release.py', 'lib/deploy_state.py',
         'lib/panel-transaction.sh', 'lib/ufw-rules.py', 'lib/verify-traffic.py',
         'lib/wg_conf.py', 'lib/verify-target.py', 'lib/hy2_bandwidth.py',
         'lib/install-sing-box.sh', 'lib/warp-watchdog.py', 'configs/sing-box-server.json.tpl')


def alive(pids):
    live = []
    for pid in pids:
        try:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if state != 'Z':
            live.append(pid)
    return live


class Run:
    def __init__(self, out, code, elapsed, hangs, timed_out, leftover):
        self.out, self.code, self.elapsed = out, code, elapsed
        self.hangs, self.timed_out, self.leftover = hangs, timed_out, leftover


def deploy(stage, group, deadline, **fake):
    """Стадия на группу узлов: ru, nl, de. fake - FAKE_* для подставного ssh."""
    with tempfile.TemporaryDirectory(prefix='stage-exit-login-hang-') as directory:
        top = Path(directory)
        root = top / 'infra'
        for sub in ('envs', 'scripts', 'lib', 'configs', 'bin', 'nodes', 'tmp'):
            (root / sub).mkdir(parents=True)
        (top / 'app/shared').mkdir(parents=True)
        (top / 'app/shared/vpn-pools.json').write_text('{}\n')
        exits = ' '.join(EXITS)
        (root / 'envs/all.env').write_text(f"EXIT_TAGS='{exits}'\nENTRY_HOST=127.0.0.1\n")
        (root / 'envs/exits.env').write_text(f"EXIT_TAGS='{exits}'\n")
        for tag in ('ru',) + EXITS:
            (root / f'envs/{tag}.env').write_text(
                f'HOST_TAG={tag}\nSSH_HOST={tag}.example\nSSH_USER=root\nSSH_PASS=fixture\n')
        for name in ('99-verify', '10-foreign'):
            (root / f'scripts/{name}.sh').write_text(STAGE)
            (root / f'scripts/{name}.sh').chmod(0o755)
        (root / 'lib/ssh.sh').write_text(source('infra/lib/ssh.sh'))
        (root / 'lib/anysda-svc.sh').write_text('apt_prepare() { :; }\n')
        (root / 'lib/fw-guard.sh').write_text('exit 0\n')
        for path in STUBS:
            (root / path).touch()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (root / 'bin' / name).write_text(text)
            (root / 'bin' / name).chmod(0o755)
        text = source('infra/deploy.sh')
        script = ('set -euo pipefail\nDEPLOY_ROOT=$PWD\nexport DEPLOY_ROOT\n'
                  'source "$DEPLOY_ROOT/lib/ssh.sh"\n'
                  + text[text.index('_STAGE_LOG='):text.index('# Без аргументов → полный pipeline.')]
                  + '\nC_R= C_G= C_Y= C_B= C_END=\nrun_stage "$1" "$2"\n')
        pids, hangs = root / 'pids', root / 'hangs'
        env = {k: v for k, v in os.environ.items()
               if k not in ('VPN2_RELEASE_MANIFEST', 'SKIPPED_EXIT_TAGS')}
        env.update({'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'TMPDIR': str(root / 'tmp'),
                    'SSH_PROBE_TIMEOUT': str(LIMIT), 'FAKE_NODES': str(root / 'nodes'),
                    'FAKE_PIDS': str(pids), 'FAKE_HANGS': str(hangs), 'FAKE_SLEEP': str(HANG)})
        env.update({f'FAKE_{k.upper()}': str(v) for k, v in fake.items()})
        log = root / 'out'
        started = time.monotonic()
        with log.open('w') as handle:
            proc = subprocess.Popen(['bash', '-c', script, 'deploy', stage, group], cwd=root,
                                    env=env, stdin=subprocess.DEVNULL, stdout=handle,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                code = proc.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait()
        elapsed = time.monotonic() - started
        pid_list = [int(p) for p in pids.read_text().split()] if pids.exists() else []
        grace = time.monotonic() + (0 if timed_out else 2)
        while alive(pid_list) and time.monotonic() < grace:
            time.sleep(0.1)
        leftover = alive(pid_list)
        for pid in leftover:
            os.kill(pid, signal.SIGKILL)
        run = Run(log.read_text(), code, elapsed,
                  hangs.read_text().split() if hangs.exists() else [], timed_out, leftover)
    print(f'{stage} {group} {fake}: exit={run.code} {run.elapsed:.1f}s '
          f'hangs={len(run.hangs)} timed_out={run.timed_out}')
    return run


class StageExitLoginHangTests(unittest.TestCase):
    def assert_hung_exit_reported(self, run):
        tail = run.out[-3000:]
        self.assertFalse(run.timed_out,
                         f'деплой ждал зависший вход на nl дольше {3 * LIMIT} с '
                         f'(3 предела по {LIMIT} с) и был снят:\n{tail}')
        self.assertLess(run.elapsed, 3 * LIMIT)
        self.assertTrue(any(HUNG.search(line) for line in run.out.splitlines()),
                        f'нет строки с именем экзита nl и «завис»:\n{tail}')
        self.assertIn('[99-verify → de] done', run.out, f'второй экзит не проверен:\n{tail}')
        self.assertIn('[de] ✓ стадия 99-verify пройдена', run.out)
        self.assertIn('[99-verify → ru] done', run.out, f'entry не проверен:\n{tail}')
        self.assertNotIn('[99-verify → nl] done', run.out)
        self.assertNotEqual(run.code, 0, f'зависший экзит дал «всё ок»:\n{tail}')
        self.assertIn('стадия 99-verify: одна или несколько нод завершились с ошибкой', run.out)
        self.assertEqual(run.leftover, [], 'ssh или .bashrc на зависшем экзите живут после стадии')

    def test_1_hung_login_on_exit_ends_by_limit(self):
        run = deploy('99-verify', 'all', 3 * LIMIT, hang_tag='nl', hang_on='all')
        self.assert_hung_exit_reported(run)

    def test_2_long_stage_after_mark_is_not_cut(self):
        run = deploy('10-foreign', 'foreign', 60, slow_tag='nl', slow=LIMIT + 3)
        tail = run.out[-3000:]
        self.assertEqual(run.code, 0, tail)
        self.assertIn(f'[nl] установка, шаг {LIMIT + 3}', run.out, f'долгая стадия оборвана:\n{tail}')
        self.assertIn('[nl] ✓ стадия 10-foreign пройдена', run.out)
        for tag in EXITS:
            self.assertIn(f'[10-foreign → {tag}] done', run.out)
            self.assertIn(f'[99-verify → {tag}] done', run.out)
        self.assertGreater(run.elapsed, LIMIT + 3)
        self.assertNotRegex(run.out, 'завис')
        self.assertEqual(run.leftover, [])

    def test_3_hang_in_push_or_stage_launch_without_retries(self):
        for where in ('push', 'script'):
            with self.subTest(where=where):
                run = deploy('99-verify', 'all', 3 * LIMIT, hang_tag='nl', hang_on=where)
                self.assert_hung_exit_reported(run)
                self.assertGreater(len(run.hangs), 0, 'заглушка ни разу не зависла')
                self.assertLess(len(run.hangs), 5, f'зависший вход повторён {len(run.hangs)} раз')

    def test_4_healthy_noisy_and_exiting_bashrc_unchanged(self):
        for fake in ({}, {'bashrc_tag': 'nl', 'bashrc': 'noise'}):
            with self.subTest(fake=fake):
                run = deploy('99-verify', 'all', 60, **fake)
                self.assertEqual(run.code, 0, run.out[-3000:])
                for tag in ('ru',) + EXITS:
                    self.assertIn(f'[99-verify → {tag}] done', run.out)
                    self.assertIn(f'[{tag}] ✓ стадия 99-verify пройдена', run.out)
                for line in NOISE:
                    self.assertNotIn(line, run.out)
                self.assertNotRegex(run.out, 'завис')
        run = deploy('99-verify', 'all', 60, bashrc_tag='nl', bashrc='exit0')
        self.assertNotEqual(run.code, 0, run.out)
        self.assertIn('shell узла не дошёл до команды', run.out)
        self.assertNotRegex(run.out, 'завис')
        self.assertNotIn('[99-verify → nl] done', run.out)
        for tag in ('ru', 'de'):
            self.assertIn(f'[99-verify → {tag}] done', run.out)
        self.assertLess(run.elapsed, 3 * LIMIT)


if __name__ == '__main__':
    unittest.main()
