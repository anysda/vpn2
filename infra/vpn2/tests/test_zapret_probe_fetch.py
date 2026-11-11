"""Зависимости видеопробы: сетевая заминка при скачивании не роняет установку.

Установщик PO-токенов и pip подменены программами: они ведут счёт запусков в
файле и падают так, как падал npm на стенде (готовая сборка canvas не скачалась
по времени, сборка из исходников без pixman-1 невозможна).
"""
import os
import sys

import pytest

from vpn2.zapret import stage

CANVAS = '''npm warn deprecated something@1.0.0: old
npm error code 1
npm error path {venv}/.pot-install.x/bgutil/server/node_modules/canvas
npm error command failed
npm error command sh -c prebuild-install -r napi || node-gyp rebuild
npm error prebuild-install warn install Request timed out
npm error gyp info it worked if it ends with ok
npm error Package pixman-1 was not found in the pkg-config search path.
npm error gyp ERR! configure error
npm error A complete log of this run can be found in: /root/.npm/_logs/debug-0.log
'''
INSTALLER = r'''#!/bin/bash
venv=$1
box={box}
count=$(( $(cat "$box/runs" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$box/runs"
ls -A "$venv" | grep -c '^\.pot-install\.' > "$box/stale.$count" || true
plan=($(cat "$box/plan"))
step=${{plan[$(( count <= ${{#plan[@]}} ? count - 1 : ${{#plan[@]}} - 1 ))]}}
work=$(mktemp -d "$venv/.pot-install.XXXXXXXX")
echo half > "$work/source.tar.gz"
case $step in
  canvas) sed "s#{{venv}}#$venv#" "$box/canvas.txt" >&2; rm -rf "$work"; exit 1 ;;
  curl) echo 'curl: (28) Operation timed out after 180001 milliseconds with 0 bytes received' >&2
        rm -rf "$work"; exit 28 ;;
  pip) echo 'ERROR: Could not find a version that satisfies the requirement bgutil-ytdlp-pot-provider==2.0.2' >&2
       rm -rf "$work"; exit 1 ;;
  sum) echo "$work/source.tar.gz: FAILED" >&2; echo 'sha256sum: WARNING: 1 computed checksum did NOT match' >&2
       rm -rf "$work"; exit 1 ;;
  killed) kill -9 $$ ;;
  silent) rm -rf "$work"; exit 3 ;;
  slow*) sleep "${{step#slow}}" ;;
esac
rm -rf "$work"
mkdir -p "$venv/bgutil"
echo 2.0.2 > "$venv/bgutil/.installed"
echo installed
'''


class Probe:
    def __init__(self, path):
        self.box = path / 'box'
        self.venv = path / 'venv'
        self.box.mkdir()
        (self.venv / 'bin').mkdir(parents=True)
        (self.venv / 'lib/node_modules/npm/bin').mkdir(parents=True)
        (self.venv / 'lib/node_modules/npm/bin/npm-cli.js').write_text('')
        self.tool('yt-dlp', f'echo {stage.YT_DLP_VER}')
        self.tool('node', f'echo {stage.NODE_VER}')
        (self.box / 'canvas.txt').write_text(CANVAS)
        self.installer = self.box / 'install-yt-pot.sh'
        self.installer.write_text(INSTALLER.format(box=self.box))
        self.pauses = []

    def tool(self, name, body):
        path = self.venv / 'bin' / name
        path.write_text(f'#!/bin/sh\n{body}\n')
        path.chmod(0o755)

    def plan(self, text):
        (self.box / 'plan').write_text(text)

    def runs(self):
        return int((self.box / 'runs').read_text())

    def stale(self, run):
        return int((self.box / f'stale.{run}').read_text())

    def leftovers(self):
        return sorted(name for name in os.listdir(self.venv) if name.startswith('.pot-install.'))

    def install(self):
        environ = {'PATH': os.environ['PATH'], 'VPN2_YT_POT_INSTALL': str(self.installer)}
        return stage.install_probe(environ, str(self.venv), sleep=self.pauses.append)


@pytest.fixture
def probe(tmp_path):
    return Probe(tmp_path)


def test_clean_install_runs_the_installer_once(probe, capfd):
    probe.plan('ok')
    assert probe.install() is True
    assert probe.runs() == 1 and probe.pauses == []
    assert 'installed' in capfd.readouterr().err
    assert probe.install() is False


@pytest.mark.parametrize(('plan', 'pauses'), [('canvas ok', [10]), ('canvas canvas ok', [10, 30]),
                                               ('curl canvas ok', [10, 30]), ('killed ok', [10])])
def test_transient_failure_is_retried(probe, plan, pauses, capfd):
    probe.plan(plan)
    assert probe.install() is True
    assert probe.runs() == len(plan.split()) and probe.pauses == pauses
    assert (probe.venv / 'bgutil/.installed').read_text() == '2.0.2\n'
    assert probe.leftovers() == []
    err = capfd.readouterr().err
    assert 'зависимости видеопробы: попытка 1 из 3 не удалась' in err
    assert 'повтор через 10 с' in err


@pytest.mark.parametrize(('step', 'what', 'cause'), [
    ('canvas', 'пакет npm canvas', 'prebuild-install warn install Request timed out'),
    ('curl', 'архив исходников bgutil-ytdlp-pot-provider', 'curl: (28) Operation timed out'),
    ('pip', 'пакет pip bgutil-ytdlp-pot-provider', 'ERROR: Could not find a version'),
    ('sum', 'архив исходников bgutil-ytdlp-pot-provider', 'source.tar.gz: FAILED'),
    ('silent', 'установщик PO-токенов', 'код выхода 3, вывода нет'),
    ('killed', 'установщик PO-токенов', 'убит сигналом 9'),
])
def test_failure_on_every_attempt_names_what_was_not_fetched(probe, step, what, cause):
    probe.plan(step)
    with pytest.raises(stage.StageError) as caught:
        probe.install()
    text = str(caught.value)
    assert probe.runs() == stage.FETCH_TRIES == 3 and probe.pauses == [10, 30]
    assert text.startswith(f'зависимости видеопробы не установились за 3 попытки: {what}: ')
    assert cause in text and '\n' not in text and len(text) < 400
    assert 'повторите ./deploy.sh' in text
    assert not (probe.venv / 'bgutil/.installed').exists()


def test_work_directory_of_a_killed_installer_is_removed_before_the_next_attempt(probe):
    probe.plan('killed killed ok')
    assert probe.install() is True
    assert [probe.stale(run) for run in (1, 2, 3)] == [0, 0, 0]
    assert probe.leftovers() == []


def test_final_failure_leaves_nothing_and_the_repeat_recovers(probe):
    probe.plan('killed killed killed ok')
    with pytest.raises(stage.StageError):
        probe.install()
    assert probe.leftovers() == []
    assert probe.install() is True
    assert probe.runs() == 4 and (probe.venv / 'bgutil/.installed').is_file()


def test_leftover_of_an_interrupted_deploy_is_removed(probe):
    stale = probe.venv / '.pot-install.abcdefgh'
    stale.mkdir()
    (stale / 'source.tar.gz').write_text('half')
    probe.plan('ok')
    assert probe.install() is True
    assert probe.stale(1) == 0 and probe.leftovers() == []


def test_slow_but_alive_installer_is_not_killed(probe):
    probe.plan('slow2')
    assert probe.install() is True
    assert probe.runs() == 1 and probe.pauses == []


def test_cli_prints_the_cause(probe, monkeypatch, capfd):
    probe.plan('canvas')
    monkeypatch.setattr(stage, 'PROBE_VENV', str(probe.venv))
    monkeypatch.setattr(stage, 'FETCH_PAUSES', (0, 0))
    monkeypatch.setenv('VPN2_YT_POT_INSTALL', str(probe.installer))
    for name in ('ZAPRET_BASE', 'ZAPRET_REF'):
        monkeypatch.delenv(name, raising=False)
    assert stage.main(['probe']) == 1
    last = capfd.readouterr().err.strip().splitlines()[-1]
    assert last.startswith('vpn2 zapret probe: зависимости видеопробы не установились за 3 попытки: '
                           'пакет npm canvas: ')
    assert 'Request timed out' in last and sys.executable not in last
