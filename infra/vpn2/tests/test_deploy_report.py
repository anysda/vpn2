"""Что видит оператор: заметка SSO, итог, backup с предупреждением, секреты экзитов, выпуск без манифеста."""
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_deploy_cli import RUNNER, environ, plays, tree  # noqa: F401 - tree - фикстура
from vpn2.deploy import report, run

NOTICE = 'SSO не включён, потому что не задан секрет.'


def fake(env, **state):
    return SimpleNamespace(env=lambda _node='ru': env, exit_tags=lambda: [], stage_log=[], verify_warnings=[],
                           deploy_warnings=[], skipped=[], **state)


@pytest.mark.parametrize('env, runtime, said', [
    ({'SSO_REQUESTED': 'true', 'SSO_CLIENT_SECRET': ' '}, False, True),
    ({'SSO_REQUESTED': 'true', 'SSO_CLIENT_SECRET': ' '}, True, False),
    ({'SSO_REQUESTED': 'true', 'SSO_CLIENT_SECRET': 'secret'}, False, False),
    ({'SSO_REQUESTED': 'false'}, False, False),
])
def test_sso_notice_conditions(env, runtime, said, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, 'ETC', tmp_path)
    if runtime:
        (tmp_path / 'sso-runtime.json').write_text('{"enabled":true}')
    report.sso_notice(fake(env))
    assert (NOTICE in capsys.readouterr().out) is said


@pytest.mark.parametrize('args, count', [((), 1), (('30-frontend', 'ru'), 1), (('30-frontend', 'all'), 1),
                                         (('23-emergency', 'foreign'), 0), (('99-verify', 'all'), 0)])
def test_sso_notice_once_at_end_of_pipeline_or_frontend_stage(tree, args, count):  # noqa: F811
    runner = RUNNER.replace('sys.exit(', "main.report.sso_notice = lambda _deploy: print('SSO-NOTICE')\nsys.exit(")
    done = subprocess.run([sys.executable, '-B', '-c', runner, *args], capture_output=True, text=True, timeout=120,
                          env=environ(tree))
    assert done.returncode == 0, done.stderr
    assert done.stdout.count('SSO-NOTICE') == count


def test_summary_names_panel_stages_and_ikev2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, 'ETC', tmp_path)
    (tmp_path / 'ikev2-mode').write_text('letsencrypt\n')
    (tmp_path / 'ikev2-server-host').write_text('vpn.example.com\n')
    deploy = fake({'SSH_HOST': '192.0.2.1', 'PANEL_DOMAIN': 'vpn.example.com', 'ADMIN_PASSWORD': 'x'})
    deploy.stage_log += ['00-bootstrap\tall\t1m 2s', '99-verify\tall\t7s']
    deploy.verify_warnings.append('[ru] ? не проверено: скорость')
    report.summary(deploy, 0)
    out = capsys.readouterr().out
    assert '⚠ деплой завершён с предупреждениями' in out and '│  00-bootstrap  all  1m 2s' in out
    assert 'Panel:    https://vpn.example.com/' in out and 'задан в config.yaml (admin.password)' in out
    assert 'IKEv2: mode=letsencrypt server-host=vpn.example.com' in out and '[ru] ? не проверено: скорость' in out


@pytest.mark.parametrize('script, code, said', [
    ("echo 'warning: сверка БД с конфигами VPN не выполнена'; exit 4", 0, 'сверка с конфигами VPN не выполнена - не проверено'),
    ("echo 'warning: расхождение'; exit 4", 0, 'БД и конфиги VPN расходятся - перевыпусти'),
    ('exit 4', 4, ''),
    ("printf '%-12s %s\\n' backup: /x.tar.gz.age", 0, ''),
    ("echo boom >&2; exit 1", 1, ''),
])
def test_backup_warning_codes(script, code, said, tmp_path, monkeypatch, capfd):
    (tmp_path / 'backup').write_text(f'#!/bin/sh\n{script}\n')
    (tmp_path / 'backup').chmod(0o755)
    monkeypatch.setattr(report, 'BACKUP', str(tmp_path / 'backup'))
    assert report.backup() == code
    assert said in capfd.readouterr().err


@pytest.mark.parametrize('sig, code', [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_backup_streams_and_signal_mid_copy_is_a_failure(tree, tmp_path, sig, code):  # noqa: F811
    script = tmp_path / 'backup'
    # Ловушка до первой строки: сигнал между echo и запуском sleep иначе ждал бы конца sleep.
    script.write_text("#!/bin/sh\ntrap 'kill $!; exit 1' INT TERM\nsleep 30 &\necho 'копирую архив'\nwait\n")
    script.chmod(0o755)
    runner = RUNNER.replace('sys.exit(', f"main.report.BACKUP = {str(script)!r}\nsys.exit(")
    with subprocess.Popen([sys.executable, '-B', '-c', runner, 'backup'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=environ(tree), start_new_session=True) as proc:
        assert proc.stdout.readline() == 'копирую архив\n'  # вывод идёт по ходу, не в конце
        os.killpg(proc.pid, sig)
        out, err = proc.communicate(timeout=15)
    assert proc.returncode == code and 'архив сохранён' not in out + err


def test_partial_secret_collection_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(run, 'INFRA', tmp_path)
    os.umask(0o022)
    deploy = run.Deploy.__new__(run.Deploy)
    deploy.tmp, deploy.exit_tags, deploy.expand = tmp_path, lambda: ['se'], lambda _group: ['se']

    def service(*_args, **_options):
        (tmp_path / 'secrets/foreign-secrets.env').write_text('SE_DIRECT=ABCDEFGHIJKLMNOP\n')
        return 1

    deploy.service = service
    with pytest.raises(SystemExit):
        deploy.pull_secrets()
    assert (tmp_path / 'secrets/foreign-secrets.env').stat().st_mode & 0o777 == 0o600


def test_unpublished_commit_is_refused_before_remote_changes(tree):  # noqa: F811
    env = environ(tree, VPN2_DEVELOPMENT='false', VPN2_RELEASE_SOURCE='http://127.0.0.1:9')
    done = subprocess.run([sys.executable, '-B', '-c', RUNNER], capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == 1 and 'Traceback' not in done.stderr
    assert not plays(tree)


def test_failed_old_layout_or_complete_never_announces_success(tree):  # noqa: F811
    done = subprocess.run([sys.executable, '-B', '-c', RUNNER], capture_output=True, text=True, timeout=120,
                          env=environ(tree, FAKE_RULES=json.dumps([['--tags complete', 2, 0]])))
    assert done.returncode == 1 and 'запись о выпуске не закрыта' in done.stderr
    assert 'деплой завершён' not in done.stdout


def test_insecure_exit_is_warned(capsys):
    deploy = run.Deploy.__new__(run.Deploy)
    deploy.nodes = {'ru': {'EXIT_TAGS': 'us de-2', 'DE_2_HY2_INSECURE': '1', 'US_HY2_INSECURE': ''}}
    deploy.warn_insecure_exits()
    said = capsys.readouterr().err
    assert '[deploy] de-2: exits[].hy2_insecure: true — сертификат экзита не проверяется' in said and 'us:' not in said
