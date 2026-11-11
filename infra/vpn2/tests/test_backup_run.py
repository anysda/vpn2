import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from vpn2.backup import env
from vpn2.backup import run as backup_run
from vpn2.backup.run import Paths, create

SCRIPT = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-backup.sh'
NEEDS_BASH = pytest.mark.skipif(
    any(shutil.which(name) is None for name in ('bash', 'sqlite3', 'tar', 'flock', 'shred', 'python3')),
    reason='нужны bash, sqlite3, tar, flock, shred, python3')
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
NAME = 'anysda-vpn2-2026-01-02T030405Z'
SECRET = 'bot-token-value'
PY = sys.executable

STUBS = {
    'age': '#!/bin/sh\n[ -z "$STUB_AGE_FAIL" ] || exit "$STUB_AGE_FAIL"\n'
           '[ -n "$STUB_AGE_NOFILE" ] || cp "$4" "$3"\n',
    'expect': '#!/bin/sh\n[ -z "$STUB_AGE_FAIL" ] || exit "$STUB_AGE_FAIL"\n'
              'line=$(grep "^spawn ")\nout=$(printf %s "$line" | cut -d\'"\' -f2)\n'
              'src=$(printf %s "$line" | cut -d\'"\' -f4)\n[ -n "$STUB_AGE_NOFILE" ] || cp "$src" "$out"\n',
    'git': '#!/bin/sh\necho abc1234\n',
    'date': '#!/bin/sh\necho 2026-01-02T030405Z\n',
    'sleep': f'#!{PY}\nimport json, os, sys\n'
             "open(os.environ['STUB_LOG'], 'a').write(json.dumps({'cmd': 'sleep', 'argv': sys.argv[1:]}) + '\\n')\n",
    'curl': f'#!{PY}\nimport json, os, sys\nargv = sys.argv[1:]\n'
            "stdin = sys.stdin.read() if '@-' in argv else ''\n"
            "open(os.environ['STUB_LOG'], 'a').write(json.dumps({'cmd': 'curl', 'argv': argv, 'stdin': stdin}) + '\\n')\n"
            "if argv[-1].endswith('/api/version'):\n    print('{\"version\":\"9.9\"}')\n",
    'aws': f'#!{PY}\nimport json, os, sys\nargv = sys.argv[1:]\n'
           "keys = {k: v for k, v in os.environ.items() if k.startswith('AWS_')}\n"
           "open(os.environ['STUB_LOG'], 'a').write(json.dumps({'cmd': 'aws', 'argv': argv, 'env': keys}) + '\\n')\n"
           "if os.environ.get('STUB_AWS_FAIL') == argv[3]:\n    print('aws: boom', file=sys.stderr)\n    sys.exit(9)\n"
           "if argv[3] == 'ls':\n    print(open(os.environ['STUB_S3_LIST']).read(), end='')\n",
}
CHECK_STUB = '''check_bundle() {
  local seq rc rest; seq=$(cat "$CHECK_DIR/seq"); rc=${seq%% *}; rest=${seq#* }
  [[ $seq == *" "* ]] || rest=$rc
  printf '%s' "$rest" > "$CHECK_DIR/seq"
  [[ $rc == 0 ]] || cat "$CHECK_DIR/text"
  return "$rc"
}
'''


def put(path, data=b'', mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    if mode:
        path.chmod(mode)
    return path


def make_node(root, *, db=True, db_bytes=None, old=0):
    """Поддельный узел в root: те же пути, что у скрипта, внутри root. Возвращает Paths."""
    paths = Paths.under(str(root))
    runtime = Path(paths.runtime_dir)
    put(runtime / 'sso-runtime.json', '{"sso": 1}\n')
    put(runtime / 'telegram-runtime.json', '{"tg": 1}\n')
    etc = Path(paths.etc_anysda)
    put(etc / 'admin-password', 'pw\n', 0o600)
    put(etc / 'tgbot-secret.txt', SECRET + '\n', 0o600)
    put(etc / 'файл с пробелом.txt', 'данные\n')
    for name in ('sso-runtime.json', 'telegram-runtime.json'):
        (etc / name).symlink_to(runtime / name)
    for name in ('wg0.conf', 'server.priv', 'server.pub'):
        put(Path(paths.wireguard) / name, name + '\n', 0o600)
    put(Path(paths.openvpn) / 'server' / '.pki.lock')
    put(Path(paths.openvpn) / 'ca.crt', 'ca\n')
    put(Path(paths.openvpn) / 'ccd' / 'client one', 'ifconfig-push\n')
    os.mkfifo(Path(paths.openvpn) / 'mgmt.fifo')
    put(Path(paths.strongswan) / 'ca.pem', 'ike\n')
    put(Path(paths.swanctl) / 'swanctl.conf', 'conns\n')
    for name in ('strongswan.conf', 'ipsec.conf', 'ipsec.secrets'):
        put(Path(paths.etc) / name, name + '\n')
    put(Path(paths.etc) / 'strongswan.d' / 'x.conf', 'x\n')
    put(Path(paths.caddy) / 'certificates' / 'сертификат b.crt', 'crt\n')
    put(Path(paths.caddy) / 'locks' / 'l.lock', 'x')
    put(Path(paths.source_root_file), '/nonexistent/src\n')
    put(Path(paths.deploy_state) / 'mgmt-ips.txt', 'exit-a 10.0.0.2\n', 0o600)
    if db_bytes is not None:
        put(Path(paths.db), db_bytes)
    elif db:
        conn = sqlite3.connect(paths.db)
        conn.execute('create table t (a)')
        conn.execute("insert into t values ('x')")
        conn.commit()
        conn.close()
    Path(paths.work_parent).mkdir(parents=True, exist_ok=True)
    local = root / 'out dir'
    local.mkdir()
    for index in range(old):
        archive = put(local / f'anysda-vpn2-2025-12-0{index + 1}T000000Z.tar.gz.age', 'old')
        os.utime(archive, (1_000 + index, 1_000 + index))
    return paths


def values(root, **extra):
    base = {'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': str(root / 'out dir'), 'BACKUP_RETENTION': '10',
            'ANYSDA_BACKUP_PASSPHRASE': 'парольная фраза'}
    return {**base, **extra}


def install_stubs(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in STUBS.items():
        put(directory / name, body, 0o755)
    return directory


def cut_script(root, dest):
    """Скрипт без check_bundle и --check-bundle (строки 75-250), пути /etc и /var/lib - под root.

    Замены (удалить вместе с bash-телом): check_bundle - заглушка CHECK_STUB; `/etc/` и `/var/lib/`
    вне heredoc с текстом FATAL становятся `<root>/etc/`, `<root>/var/lib/`.
    """
    lines = SCRIPT.read_text().split('\n')
    cut = lines[:74] + CHECK_STUB.split('\n') + lines[250:]
    result, heredoc = [], False
    for line in cut:
        if line.startswith('\tEOM'):
            heredoc = False
        if not heredoc:
            line = re.sub(r'(?<![\w$./-])/(etc|var/lib)/', lambda m: f'{root}/{m.group(1)}/', line)
        if 'cat >&2 <<-EOM' in line:
            heredoc = True
        result.append(line)
    dest.write_text('\n'.join(result) + '\n')
    return dest


def norm(text, root):
    text = text.replace(str(root), '<R>')
    text = re.sub(r'\.backup-tmp\.\w+', '.backup-tmp.<X>', text)
    return re.sub(r'"env": (\{[^}]*\})', lambda m: '"env": ' + json.dumps(sorted(json.loads(m.group(1)).items())), text)


def placeholders(out, final):
    """size и hash в итоге зависят от mtime внутри tar: сверяем с файлом и подменяем меткой."""
    if not final.is_file() or 'size:' not in out:
        return out
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    size = f"{'size:':<12} {final.stat().st_size} bytes\n"
    assert size in out
    assert f"{'hash:':<12} sha256:{digest}\n" in out
    # Цифры размера случайно встречаются и внутри хэша: размер меняем только в его строке.
    return out.replace(size, f"{'size:':<12} <SIZE> bytes\n").replace(digest, '<HASH>')


def test_placeholders_keep_hash_that_contains_size_digits(tmp_path):
    final = tmp_path / 'archive'
    final.write_bytes(b'12345')
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    assert '5' in digest
    out = f"{'size:':<12} 5 bytes\n{'hash:':<12} sha256:{digest}\n"
    assert placeholders(out, final) == f"{'size:':<12} <SIZE> bytes\n{'hash:':<12} sha256:<HASH>\n"


def snapshot(root, rc, out, err, log):
    """Всё наблюдаемое после запуска, пути root заменены на <R>."""
    local = root / 'out dir'
    final = local / f'{NAME}.tar.gz.age'
    members, manifest = [], None
    if final.is_file():
        with tarfile.open(final, 'r:gz') as tar:
            for member in tar:
                data = tar.extractfile(member).read() if member.isfile() else b''
                members.append((member.name, oct(member.mode), member.type, member.size, member.linkname, data))
                if member.name == f'{NAME}/manifest.json':
                    manifest = json.loads(data)
    out = placeholders(out, final)
    calls = [norm(line, root) for line in log.read_text().splitlines()] if log.exists() else []
    work = Path(Paths.under(str(root)).work_parent)
    return {'rc': rc, 'out': norm(out, root), 'err': norm(err, root), 'members': members, 'manifest': manifest,
            'local': sorted(p.name for p in local.iterdir()), 'mode': oct(final.stat().st_mode) if final.exists() else None,
            'calls': calls, 'leftover': sorted(p.name for p in work.glob('.backup-tmp.*'))}


def write_env(root, vals):
    lines = '\n'.join(f'{key}={shlex.quote(value)}' for key, value in vals.items())
    put(Path(Paths.under(str(root)).env_file), lines + '\n', 0o600)


def run_bash(root, scenario):
    bin_dir = install_stubs(root / 'bin')
    check = root / 'check'
    put(check / 'seq', ' '.join(scenario.get('check', ['0'])))
    put(check / 'text', scenario.get('check_text', ''))
    script = cut_script(root, root / 'backup.sh')
    log = root / 'log'
    environ = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'HOME': str(root), 'LC_ALL': 'C.UTF-8', 'STUB_LOG': str(log),
               'CHECK_DIR': str(check), 'STUB_S3_LIST': str(root / 's3list'),
               **{k: v for k, v in scenario.get('stub_env', {}).items()}}
    done = subprocess.run(['bash', str(script)], env=environ, capture_output=True, stdin=subprocess.DEVNULL, check=False)
    return snapshot(root, done.returncode, done.stdout.decode(), done.stderr.decode(), log)


def run_python(root, vals, scenario, monkeypatch):
    paths = Paths.under(str(root))
    bin_dir = install_stubs(root / 'bin')
    log = root / 'log'
    for key in [k for k in os.environ if k.startswith(('AWS_', 'STUB_'))]:
        monkeypatch.delenv(key)
    monkeypatch.setenv('PATH', f'{bin_dir}:/usr/bin:/bin')
    monkeypatch.setenv('STUB_LOG', str(log))
    monkeypatch.setenv('STUB_S3_LIST', str(root / 's3list'))
    for key, value in scenario.get('stub_env', {}).items():
        monkeypatch.setenv(key, value)
    seq = list(scenario.get('check', ['0']))

    def check(_bundle, out, _err):
        code = int(seq.pop(0) if len(seq) > 1 else seq[0])
        if code != 0:
            out.write(scenario.get('check_text', ''))
        return code

    def sleep(seconds):
        with open(log, 'a') as handle:
            handle.write(json.dumps({'cmd': 'sleep', 'argv': [str(seconds)]}) + '\n')

    out, err = StringIO(), StringIO()
    settings = env.settings(vals, paths.env_file)
    rc = create(settings, paths=paths, out=out, err=err, now=NOW, check=check, sleep=sleep)
    return snapshot(root, rc, out.getvalue(), err.getvalue(), log)


S3 = {'BACKUP_BACKEND': 's3', 'BACKUP_S3_ENDPOINT': 'https://s3.example.test', 'BACKUP_S3_BUCKET': 'bkt',
      'ANYSDA_S3_ACCESS_KEY': 'access', 'ANYSDA_S3_SECRET_KEY': 'shh', 'BACKUP_RETENTION': '2'}
LISTING = ''.join(f'2025-12-0{i} 00:00:00  10 anysda-vpn2-2025-12-0{i}T000000Z.tar.gz.age\n' for i in (1, 2, 3)) \
    + f'2026-01-02 03:04:05  10 {NAME}.tar.gz.age\n2025-01-01 00:00:00  3 other.txt\n'
MISMATCH = 'устройство 12 «Телефон»: нет пира\nустройство 3: нет учётки\nустройство 12: дубль\n'
MANY = ''.join(f'устройство {n}: нет пира\n' for n in range(1, 14))
# (имя, значения env, число старых архивов, сценарий, код выхода bash)
SCENARIOS = [
    ('success', {}, 0, {}, 0),
    ('empty-passphrase', {'ANYSDA_BACKUP_PASSPHRASE': ''}, 0, {}, 2),
    ('sqlite-fails', {}, 0, {'db_bytes': b'not a database'}, 1),
    ('age-fails', {}, 0, {'stub_env': {'STUB_AGE_FAIL': '7'}}, 7),
    ('age-no-file', {}, 0, {'stub_env': {'STUB_AGE_NOFILE': '1'}}, 3),
    ('rotation-3-old', {'BACKUP_RETENTION': '2'}, 3, {}, 0),
    ('s3-ok-rotation', S3, 1, {'s3list': LISTING}, 0),
    ('s3-upload-fails', S3, 0, {'s3list': LISTING, 'stub_env': {'STUB_AWS_FAIL': 'cp'}}, 9),
    ('s3-list-empty', S3, 0, {'s3list': ''}, 1),
    ('s3-rm-fails', S3, 0, {'s3list': LISTING, 'stub_env': {'STUB_AWS_FAIL': 'rm'}}, 9),
    ('warning-no-wg', {}, 0, {'check': ['5'], 'check_text': 'нет wg\nещё строка\n'}, 4),
    ('warning-other-code', {}, 0, {'check': ['1']}, 4),
    ('retry-then-ok', {}, 0, {'check': ['3', '3', '0'], 'check_text': MISMATCH}, 0),
    ('ten-mismatches', {}, 0, {'check': ['3'], 'check_text': MISMATCH}, 4),
    ('ten-mismatches-many-ids', {}, 0, {'check': ['3'], 'check_text': MANY}, 4),
    ('no-db', {}, 0, {'db': False}, 0),
]


# удалить вместе с bash-телом: прямая сверка с anysda-backup.sh на одном и том же поддельном узле
@NEEDS_BASH
@pytest.mark.parametrize(('label', 'extra', 'old', 'scenario', 'code'), SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_create_matches_bash(tmp_path, monkeypatch, label, extra, old, scenario, code):
    results = []
    for who in ('bash', 'python'):
        root = tmp_path / 'node'  # тот же путь для обоих: backup.env и source-root попадают в архив
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir()
        make_node(root, db=scenario.get('db', True), db_bytes=scenario.get('db_bytes'), old=old)
        put(root / 's3list', scenario.get('s3list', ''))
        vals = values(root, **extra)
        write_env(root, vals)
        results.append(run_bash(root, scenario) if who == 'bash'
                       else run_python(root, vals, scenario, monkeypatch))
    shell, python = results
    assert shell['rc'] == code, label
    if label == 'empty-passphrase':
        shell = deviate_single_event(shell)
    assert python == shell


def deviate_single_event(result):
    """Единственное отличие: тревоги боту на 8877 больше нет, о сбое говорит одно событие backup_failed."""
    calls = [line for line in result['calls'] if f'X-Tgbot-Secret: {SECRET}' not in json.loads(line)['argv']]
    assert len(calls) == len(result['calls']) - 1
    return {**result, 'calls': calls}


# ---- проверки функций и краёв без bash ----

def node(tmp_path, monkeypatch, **kw):
    root = tmp_path / 'n'
    root.mkdir()
    paths = make_node(root, **kw)
    bin_dir = install_stubs(root / 'bin')
    monkeypatch.setenv('PATH', f'{bin_dir}:/usr/bin:/bin')
    monkeypatch.setenv('STUB_LOG', str(root / 'log'))
    return root, paths


def go(root, paths, **kw):
    vals = values(root)
    write_env(root, vals)
    out, err = StringIO(), StringIO()
    rc = create(env.settings(vals, paths.env_file), paths=paths, out=out, err=err, now=NOW,
                check=lambda _b, _o, _e: 0, sleep=lambda _s: None, **kw)
    return rc, out.getvalue(), err.getvalue()


def events(root):
    log = root / 'log'
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [r for r in records if r['cmd'] == 'curl']


def test_create_success_and_names_with_space(tmp_path, monkeypatch):
    root, paths = node(tmp_path, monkeypatch)
    rc, out, _err = go(root, paths)
    final = root / 'out dir' / f'{NAME}.tar.gz.age'
    assert rc == 0
    assert final.is_file()
    assert 'hash:' in out
    assert not list(Path(paths.work_parent).glob('.backup-tmp.*'))


def test_create_busy_lock_reports_and_cleans(tmp_path, monkeypatch):
    root, paths = node(tmp_path, monkeypatch)
    fd = os.open(paths.pki_lock, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, _out, _err = go(root, paths, lock_wait=0.2)
    finally:
        os.close(fd)
    assert rc == 1
    assert any('замок PKI OpenVPN занят дольше минуты' in ' '.join(e['argv']) for e in events(root))
    assert not list(Path(paths.work_parent).glob('.backup-tmp.*'))


def test_create_interrupt_leaves_no_temp(tmp_path, monkeypatch):
    root, paths = node(tmp_path, monkeypatch)
    real = subprocess.run

    def boom(args, **kwargs):
        if args[0] == 'tar':
            raise KeyboardInterrupt
        return real(args, **kwargs)

    vals = values(root)
    write_env(root, vals)
    with pytest.raises(KeyboardInterrupt):
        create(env.settings(vals, paths.env_file), paths=paths, run=boom, out=StringIO(), err=StringIO(), now=NOW,
               check=lambda _b, _o, _e: 0, sleep=lambda _s: None)
    assert not list(Path(paths.work_parent).glob('.backup-tmp.*'))


def test_report_failure_keeps_secret_out_of_argv(tmp_path):
    paths = Paths.under(str(tmp_path))
    put(Path(paths.secret_file), SECRET + '\n')
    calls = []
    state = backup_run._State()
    state.warning = 'пир не найден'
    backup_run.report_failure(4, state, paths, lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert SECRET not in ' '.join(argv)
    assert kw['input'] == f'Authorization: Bearer {SECRET}\n'.encode()
    assert 'backup_warning' in argv[argv.index('-d') + 1]
    calls.clear()
    backup_run.report_failure(0, state, paths, lambda *a, **_k: calls.append(a))
    assert not calls


def test_empty_passphrase_is_one_russian_event(tmp_path, monkeypatch):
    root, paths = node(tmp_path, monkeypatch)
    vals = values(root, ANYSDA_BACKUP_PASSPHRASE='')
    err = StringIO()
    assert create(env.settings(vals, paths.env_file), paths=paths, out=StringIO(), err=err, now=NOW) == 2
    assert err.getvalue().startswith('anysda-backup FATAL: ANYSDA_BACKUP_PASSPHRASE отсутствует или пуста.')
    assert paths.env_file in err.getvalue()
    sent = events(root)
    assert len(sent) == 1 and SECRET not in ' '.join(sent[0]['argv'])
    assert sent[0]['argv'][-1] == backup_run.notify.PANEL_URL
    assert json.loads(sent[0]['argv'][sent[0]['argv'].index('-d') + 1]) == {
        'type': 'backup_failed', 'detail': 'нет пароля шифрования'}
    assert sent[0]['stdin'] == f'Authorization: Bearer {SECRET}\n'


def _no_space(args, **kwargs):
    if args[0] == 'tar':
        return subprocess.CompletedProcess(args, 2, b'', b'tar: write error: No space left on device\n')
    return subprocess.run(args, **kwargs)


@pytest.mark.parametrize(('extra', 'stub_env', 'run', 'code'), [
    ({}, {}, _no_space, 2),
    ({}, {'STUB_AGE_FAIL': '7'}, subprocess.run, 7),
    (S3, {'STUB_AWS_FAIL': 'cp'}, subprocess.run, 9),
], ids=['no-space', 'encrypt-fails', 'upload-target-down'])
def test_failure_at_any_step_is_one_backup_failed_event(tmp_path, monkeypatch, extra, stub_env, run, code):
    """На каком бы шаге ни оборвался бэкап, панель получает ровно одно backup_failed - его она шлёт в Telegram."""
    root, paths = node(tmp_path, monkeypatch)
    put(root / 's3list', '')
    monkeypatch.setenv('STUB_S3_LIST', str(root / 's3list'))
    for key, value in stub_env.items():
        monkeypatch.setenv(key, value)
    vals = values(root, **extra)
    write_env(root, vals)
    rc = create(env.settings(vals, paths.env_file), paths=paths, run=run, out=StringIO(), err=StringIO(), now=NOW,
                check=lambda _b, _o, _e: 0, sleep=lambda _s: None)
    assert rc == code
    posted = [e for e in events(root) if e['argv'][-1] == backup_run.notify.PANEL_URL]
    assert len(posted) == 1
    assert json.loads(posted[0]['argv'][posted[0]['argv'].index('-d') + 1]) == {
        'type': 'backup_failed', 'detail': f'код выхода {code}'}
    assert not list(Path(paths.work_parent).glob('.backup-tmp.*'))


def test_success_posts_no_event(tmp_path, monkeypatch):
    root, paths = node(tmp_path, monkeypatch)
    assert go(root, paths)[0] == 0
    assert not [e for e in events(root) if e['argv'][-1] == backup_run.notify.PANEL_URL]


def test_rotate_local_keeps_newest(tmp_path):
    for index in range(4):
        archive = put(tmp_path / f'anysda-vpn2-{index}.tar.gz.age', 'x')
        os.utime(archive, (100 + index, 100 + index))
    put(tmp_path / 'other.txt', 'x')
    final = tmp_path / 'anysda-vpn2-3.tar.gz.age'
    out = StringIO()
    backup_run.rotate_local(str(tmp_path), str(final), 2, out)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        'anysda-vpn2-2.tar.gz.age', 'anysda-vpn2-3.tar.gz.age', 'other.txt']
    assert out.getvalue().count('rotated out') == 2


def test_default_check_without_db_passes(tmp_path):
    pytest.importorskip('vpn2.backup.check')
    bundle = tmp_path / 'b'
    bundle.mkdir()
    assert backup_run._default_check(str(bundle), StringIO(), StringIO()) == 0
    assert not (bundle / 'manifest.json').exists()
