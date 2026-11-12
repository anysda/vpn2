#!/usr/bin/env python3
"""Слепок хоста для стенда CI: сбор, вычитание базы, правила игнорирования, diff (только stdlib).

Запуск: snapshot.py [--out ФАЙЛ] [--baseline ФАЙЛ] [--root КАТАЛОГ] или snapshot.py diff СТАРЫЙ НОВЫЙ.
На узел стенда файл копирует infra/tests/ci-stand.sh; в пакет vpn2 он не входит.

Слепок - {раздел: {имя: {поле: значение}}} плюс служебный ключ "schema".
"""
import argparse
import fnmatch
import glob
import grp
import gzip
import hashlib
import io
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_KEY = 'schema'
_IGNORE_KEYS = frozenset(('section', 'name', 'field', 'reason'))
_MIN_REASON = 10


class SnapshotError(Exception):
    """Слепок или файл игнорирования повреждён."""


def load_ignore(path):
    """Прочитать YAML-список правил игнорирования {section, name, field, reason}.

    name - маска fnmatch.fnmatchcase; её "*" переходит и через "/", поэтому
    /etc/wireguard/*.conf подходит и к /etc/wireguard/sub/x.conf.
    field - имя поля или "*" (вся запись, включая её наличие); "поле:ключ" убирает из списка поля строки "ключ=...".
    Нет ключа, лишний ключ, reason короче 10 символов или файл не список -
    SnapshotError с номером записи (с 1). Пустой файл даёт пустой список.
    """
    import yaml
    try:
        data = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    except yaml.YAMLError as error:
        raise SnapshotError(f'{path}: {error}') from None
    if data is None:
        return []
    if not isinstance(data, list):
        raise SnapshotError(f'{path}: expected a list of ignore records')
    for number, entry in enumerate(data, 1):
        if not isinstance(entry, dict):
            raise SnapshotError(f'{path}: record {number}: not a mapping')
        missing = sorted(_IGNORE_KEYS - entry.keys())
        if missing:
            raise SnapshotError(f'{path}: record {number}: missing {", ".join(missing)}')
        extra = sorted(str(key) for key in entry.keys() - _IGNORE_KEYS)
        if extra:
            raise SnapshotError(f'{path}: record {number}: unexpected {", ".join(extra)}')
        for key in ('section', 'name', 'field'):
            if not isinstance(entry[key], str) or not entry[key]:
                raise SnapshotError(f'{path}: record {number}: {key} must be a non-empty string')
        reason = entry['reason']
        if not isinstance(reason, str) or len(reason.strip()) < _MIN_REASON:
            raise SnapshotError(
                f'{path}: record {number}: reason must be at least {_MIN_REASON} characters')
    return data


def _sections(snapshot):
    return [key for key in snapshot if key != SCHEMA_KEY]


def subtract(snapshot, baseline):
    """Оставить записи, которых нет в базе или которые от неё отличаются.

    Запись из базы, пропавшая из слепка, остаётся как {"absent": True}.
    Входные данные не меняются.
    """
    result = {}
    if SCHEMA_KEY in snapshot:
        result[SCHEMA_KEY] = snapshot[SCHEMA_KEY]
    for section in sorted(set(_sections(snapshot)) | set(_sections(baseline))):
        mine = snapshot.get(section, {})
        base = baseline.get(section, {})
        kept = {}
        for name, record in mine.items():
            if name not in base or base[name] != record:
                kept[name] = json.loads(json.dumps(record))
        for name in base:
            if name not in mine:
                kept[name] = {'absent': True}
        if kept:
            result[section] = kept
    return result


def _matches(rule, section, name):
    return rule['section'] == section and fnmatch.fnmatchcase(name, rule['name'])


def _visible(section, name, record, ignore):
    """Запись без игнорируемых полей или None, если правило "*" скрывает её целиком."""
    hidden = set()
    dropped = []
    for rule in ignore:
        if not _matches(rule, section, name):
            continue
        if rule['field'] == '*':
            return None
        if ':' in rule['field']:
            dropped.append(rule['field'].split(':', 1))
        else:
            hidden.add(rule['field'])
    result = {key: value for key, value in record.items() if key not in hidden}
    for field, key in dropped:
        if isinstance(result.get(field), list):
            result[field] = [item for item in result[field]
                             if not (isinstance(item, str) and item.startswith(f'{key}='))]
    return result


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def diff(old, new, ignore=()):
    """Отсортированный список различий двух слепков; пустой, если равны.

    Строки: "<section> <name>: нет в текущем", "<section> <name>: лишнее в текущем",
    "<section> <name> <field>: <old> -> <new>" (у стороны без поля - нет).
    Разная схема даёт единственную строку "schema: 1 -> 2" и ничего больше.
    """
    if old.get(SCHEMA_KEY) != new.get(SCHEMA_KEY):
        return [f'{SCHEMA_KEY}: {_dumps(old.get(SCHEMA_KEY))} -> {_dumps(new.get(SCHEMA_KEY))}']
    lines = []
    for section in set(_sections(old)) | set(_sections(new)):
        before = old.get(section, {})
        after = new.get(section, {})
        for name in set(before) | set(after):
            left = _visible(section, name, before[name], ignore) if name in before else None
            right = _visible(section, name, after[name], ignore) if name in after else None
            if name in before and left is None or name in after and right is None:
                continue
            if left is None:
                lines.append(f'{section} {name}: лишнее в текущем')
            elif right is None:
                lines.append(f'{section} {name}: нет в текущем')
            else:
                for field in set(left) | set(right):
                    if field in left and field in right and left[field] == right[field]:
                        continue
                    was = _dumps(left[field]) if field in left else 'нет'
                    now = _dumps(right[field]) if field in right else 'нет'
                    lines.append(f'{section} {name} {field}: {was} -> {now}')
    return sorted(lines)


def unused_ignore(old, new, ignore):
    """Правила игнорирования, не совпавшие ни с одним именем (и полем) ни в old, ни в new."""
    unused = []
    for rule in ignore:
        used = False
        for snapshot in (old, new):
            for name, record in snapshot.get(rule['section'], {}).items():
                if not fnmatch.fnmatchcase(name, rule['name']):
                    continue
                if rule['field'] == '*' or rule['field'] in record:
                    used = True
                elif ':' in rule['field']:
                    field, key = rule['field'].split(':', 1)
                    items = record.get(field)
                    if isinstance(items, list) and any(
                            isinstance(item, str) and item.startswith(f'{key}=') for item in items):
                        used = True
        if not used:
            unused.append(rule)
    return unused


def dump(snapshot, path):
    """Записать слепок как JSON с отсортированными ключами; путь .gz сжимается с mtime 0 (байты стабильны)."""
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, indent=1).encode('utf-8')
    if str(path).endswith('.gz'):
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0) as packed:
            packed.write(raw)
        raw = buffer.getvalue()
    descriptor, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=Path(path).parent)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(raw)
    os.replace(temporary, path)


def load(path):
    """Прочитать слепок, записанный dump."""
    raw = Path(path).read_bytes()
    if str(path).endswith('.gz'):
        raw = gzip.decompress(raw)
    return json.loads(raw.decode('utf-8'))


SCHEMA = 1
_TIMEOUT = 60
_NOT_INSTALLED = 127
_UNITS_TYPES = 'service,timer,socket,path,mount'
_SECRET_NAME = re.compile('PASS|SECRET|TOKEN|KEY|PRIVATE|HASH', re.IGNORECASE)
# Переменные базового образа меняются при его обновлении и к установке не относятся.
_IMAGE_ENV_NAMES = ('PATH', 'LANG', 'GPG_KEY', 'NODE_VERSION', 'YARN_VERSION')
_IMAGE_ENV_PREFIX = 'PYTHON_'
_COUNTERS = re.compile(r'packets \d+ bytes \d+')
_USERS = re.compile(r'\("([^"]*)",pid=')
_VERIFY_REPORT = 'var/lib/anysda/verify-report.json'
# Конфиги со вписанными секретами: sha256 у них случайный, сверяется shape -
# содержимое, где значения секретных полей заменены маской.
_SHAPED = ('/etc/sing-box/config*.json', '/etc/anysda/anysda-config.yaml',
           '/etc/anysda/failover-watchdog.env', '/etc/wireguard/wg0.conf',
           '/opt/anysda-vpn2/config.yaml', '/opt/anysda-vpn2/infra/envs/*.env',
           '/var/lib/vpn2-apply/*.good', '/opt/AdGuardHome*/AdGuardHome.yaml')
_SECRET_FIELD = re.compile('pass|secret|token|key|private|hash|uuid|short_id|psk|salt|reserved', re.IGNORECASE)
_ASSIGN = re.compile(r'''^((\s*)(?:-\s+)?(?:export\s+)?["']?([\w.-]+)["']?\s*[:=]\s*)(.*)$''')
_MASK = '*'
# Каталоги данных служб: сверяются сам каталог, его права и владелец, содержимое живое.
_SHALLOW = ('var/lib/sing-box', 'var/lib/vmsingle', 'var/lib/caddy')


def _run(argv):
    """Запустить команду: (код, stdout); stderr отброшен, LC_ALL=C, предел 60 с."""
    try:
        done = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=_TIMEOUT,
            env=dict(os.environ, LC_ALL='C'), check=False)
    except FileNotFoundError:
        return _NOT_INSTALLED, ''
    except PermissionError:
        return 126, ''
    except subprocess.TimeoutExpired:
        return 124, ''
    return done.returncode, done.stdout.decode('utf-8', 'replace')


def _failed(argv, code):
    return {'_error': {'reason': f'{" ".join(argv)}: код {code}'}}


def _owner(uid, gid):
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = str(gid)
    return f'{user}:{group}'


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path, info):
    kind = ('file' if stat.S_ISREG(info.st_mode) else 'dir' if stat.S_ISDIR(info.st_mode)
            else 'link' if stat.S_ISLNK(info.st_mode) else 'other')
    record = {'type': kind, 'mode': format(stat.S_IMODE(info.st_mode), '04o'),
              'owner': _owner(info.st_uid, info.st_gid)}
    try:
        if kind == 'file':
            record['sha256'] = _sha256(path)
        elif kind == 'link':
            record['target'] = os.readlink(path)
    except OSError as error:
        record['error'] = error.strerror or type(error).__name__
    return record


def _masked(value):
    if isinstance(value, dict):
        return {key: _MASK if _SECRET_FIELD.search(key) else _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


def _masked_lines(text):
    """Строки "ключ: значение" и "ключ=значение": у секретного ключа значение - маска.
    Блочное значение YAML (| или >) секретного ключа уходит целиком."""
    lines = []
    block = None
    for line in text.splitlines():
        if block is not None:
            if not line.strip() or len(line) - len(line.lstrip()) > block:
                continue
            block = None
        found = _ASSIGN.match(line)
        if found and _SECRET_FIELD.search(found.group(3)):
            if found.group(4)[:1] in ('|', '>'):
                block = len(found.group(2))
            line = found.group(1) + _MASK
        lines.append(line)
    return '\n'.join(lines)


def _accounts(text, keep):
    """Строки passwd или group без номеров: их выдаёт счётчик системы, не установка."""
    rows = [line.split(':') for line in text.splitlines() if line.strip()]
    return '\n'.join(sorted(':'.join(row[index] for index in keep if index < len(row)) for row in rows))


def _callers(text, root):
    """Список uid службы управления именами: номера зависят от порядка создания пользователей."""
    names = {}
    with open(os.path.join(root, 'etc/passwd'), encoding='utf-8', errors='replace') as stream:
        for line in stream:
            row = line.split(':')
            if len(row) > 2:
                names[row[2]] = row[0]
    return json.dumps(sorted(names.get(str(uid), str(uid)) for uid in json.loads(text)))


def _shape(name, path, root):
    """sha256 содержимого без случайного (секреты, номера пользователей) или None."""
    special = {'/etc/passwd': lambda text: _accounts(text, (0, 5, 6)),
               '/etc/passwd-': lambda text: _accounts(text, (0, 5, 6)),
               '/etc/group': lambda text: _accounts(text, (0, 3)),
               '/etc/group-': lambda text: _accounts(text, (0, 3)),
               '/etc/vpn2-control/callers.json': lambda text: _callers(text, root)}
    if name not in special and not any(fnmatch.fnmatchcase(name, mask) for mask in _SHAPED):
        return None
    with open(path, encoding='utf-8', errors='replace') as stream:
        text = stream.read()
    if name in special:
        normal = special[name](text)
    else:
        try:
            normal = json.dumps(_masked(json.loads(text)), sort_keys=True, ensure_ascii=False)
        except ValueError:
            normal = _masked_lines(text)
    return hashlib.sha256(normal.encode()).hexdigest()


def _walk(path, root, records, deep=True):
    try:
        info = os.lstat(path)
    except OSError:
        return
    name = '/' + os.path.relpath(path, root).replace(os.sep, '/')
    record = _file_record(path, info)
    if stat.S_ISREG(info.st_mode) and 'error' not in record:
        try:
            shape = _shape(name, path, root)
        except (OSError, ValueError, TypeError) as error:
            record['error'] = f'shape: {type(error).__name__}'
        else:
            if shape:
                record['shape'] = shape
    if stat.S_ISDIR(info.st_mode) and deep:
        try:
            children = sorted(os.listdir(path))
        except OSError as error:
            record['error'] = error.strerror or type(error).__name__
            children = []
    else:
        children = []
    records[name] = record
    for child in children:
        if child != '__pycache__':
            _walk(os.path.join(path, child), root, records)


def _files(root):
    """Файлы, каталоги и ссылки под /etc, /usr/local/bin, /usr/local/sbin, /opt/anysda*,
    /var/lib/anysda*, /var/lib/vpn2* в root; конфиг и бинарь AdGuard Home; каталоги
    данных служб (_SHALLOW) без содержимого."""
    records = {}
    roots = [os.path.join(root, 'etc'), os.path.join(root, 'usr/local/bin'),
             os.path.join(root, 'usr/local/sbin')]
    for mask in ('opt/anysda*', 'var/lib/anysda*', 'var/lib/vpn2*'):
        roots.extend(sorted(glob.glob(os.path.join(root, mask))))
    for path in roots:
        _walk(path, root, records)
    for home in ('opt/AdGuardHome', 'opt/AdGuardHome-unfiltered'):
        _walk(os.path.join(root, home), root, records, deep=False)
        for name in ('AdGuardHome', 'AdGuardHome.yaml'):
            _walk(os.path.join(root, home, name), root, records)
    for name in _SHALLOW:
        _walk(os.path.join(root, name), root, records, deep=False)
    return records


def _table(lines):
    """Строки без шапки: [(колонки)]; ведущий маркер "●"/"*" отбрасывается."""
    rows = []
    for line in lines.splitlines():
        columns = line.split()
        if columns and columns[0] in ('●', '*'):
            columns = columns[1:]
        if columns:
            rows.append(columns)
    return rows


def _service_props(out):
    """Блоки "Id=...", "Type=...", "Restart=..." через пустую строку: {служба: {свойство: значение}}."""
    props = {}
    for block in out.split('\n\n'):
        pairs = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if 'Id' in pairs:
            props[pairs['Id']] = pairs
    return props


def _runs_once(name, records, props):
    """Служба, что отрабатывает и выходит: oneshot, служба таймера, Type=idle без перезапуска (dmesg)."""
    if not name.endswith('.service'):
        return False
    kind = props.get(name, {})
    return (f'{name[:-len(".service")]}.timer' in records or kind.get('Type') == 'oneshot'
            or (kind.get('Type') == 'idle' and kind.get('Restart') == 'no'))


def _units(run):
    """Юниты systemd: enabled из list-unit-files, active из list-units.
    Слепок не зависит от момента: inactive не пишется (загружен и стоит = не загружен),
    у разовой службы пишется только то, что systemd удерживает, - failed и active/exited;
    её проходные состояния (у Type=idle фазы запуска видны как active) не пишутся."""
    files = ['systemctl', 'list-unit-files', '--no-legend', '--plain', f'--type={_UNITS_TYPES}']
    loaded = ['systemctl', 'list-units', '--all', '--no-legend', '--plain',
              f'--type={_UNITS_TYPES}']
    code, out = run(files)
    if code != 0:
        return _failed(files, code)
    records = {columns[0]: {'enabled': columns[1]} for columns in _table(out) if len(columns) > 1}
    code, out = run(loaded)
    if code != 0:
        return _failed(loaded, code)
    states = {columns[0]: columns[2:4] for columns in _table(out) if len(columns) > 3}
    show = ['systemctl', 'show', '--property=Id,Type,Restart', '*.service']
    code, out = run(show)
    if code != 0:
        return _failed(show, code)
    props = _service_props(out)
    names = set(records) | set(states)
    for name, (active, sub) in states.items():
        if active == 'inactive':
            continue
        if _runs_once(name, names, props) and not (active == 'failed' or (active, sub) == ('active', 'exited')):
            continue
        records.setdefault(name, {})['active'] = active
    return records


def _ports(run):
    """Слушающие сокеты: имя "tcp 0.0.0.0:443 nginx" (процессы через запятую), поле process."""
    argv = ['ss', '-H', '-lntu', '-p']
    code, out = run(argv)
    if code != 0:
        return _failed(argv, code)
    names = {}
    for columns in _table(out):
        if len(columns) < 5:
            continue
        found = names.setdefault(f'{columns[0]} {columns[4]}', set())
        found.update(_USERS.findall(' '.join(columns[5:])))
    records = {}
    for name, found in names.items():
        process = ','.join(sorted(found))
        records[f'{name} {process}' if process else name] = {'process': process}
    return records


def _nft(out):
    records = {}
    depth = 0
    table = chain = None
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith('{'):
            words = line.split()
            if depth == 0 and words[0] == 'table' and len(words) >= 3:
                table = f'{words[1]} {words[2]}'
            elif depth == 1 and words[0] == 'chain' and table:
                chain = f'nft {table} {words[1]}'
                records[chain] = {'rules': []}
            elif depth == 2 and chain:
                records[chain]['rules'].append(_COUNTERS.sub('packets 0 bytes 0', line))
            depth += 1
        elif line == '}':
            depth -= 1
            if depth == 1:
                chain = None
            elif depth == 0:
                table = None
        elif depth == 2 and chain:
            records[chain]['rules'].append(_COUNTERS.sub('packets 0 bytes 0', line))
    return records


def _firewall(run):
    """nftables по цепочкам и вывод ufw."""
    records = {}
    errors = []
    for argv in (['nft', 'list', 'ruleset'], ['ufw', 'status', 'verbose']):
        code, out = run(argv)
        if code == _NOT_INSTALLED:
            continue
        if code != 0:
            errors.append(_failed(argv, code)['_error']['reason'])
        elif argv[0] == 'nft':
            records.update(_nft(out))
        else:
            records['ufw'] = {'status': [line.strip() for line in out.splitlines() if line.strip()]}
    if errors:
        records['_error'] = {'reason': '; '.join(errors)}
    return records


def _container(item):
    config = item.get('Config') or {}
    host = item.get('HostConfig') or {}
    ports = []
    for container_port, bindings in ((item.get('NetworkSettings') or {}).get('Ports') or {}).items():
        for binding in bindings or []:
            ports.append(f'{binding.get("HostIp", "")}:{binding.get("HostPort", "")}->{container_port}')
    mounts = [f'{mount.get("Source", "")}:{mount.get("Destination", "")}:'
              f'{"rw" if mount.get("RW", True) else "ro"}' for mount in item.get('Mounts') or []]
    env = []
    for entry in config.get('Env') or []:
        name, _, value = entry.partition('=')
        if name in _IMAGE_ENV_NAMES or name.startswith(_IMAGE_ENV_PREFIX):
            continue
        env.append(f'{name}=<скрыто>' if _SECRET_NAME.search(name) else f'{name}={value}')
    return {
        'image': config.get('Image', ''), 'image_id': item.get('Image', ''),
        'state': (item.get('State') or {}).get('Status', ''),
        'restart': (host.get('RestartPolicy') or {}).get('Name', ''),
        'network': host.get('NetworkMode', ''),
        'ports': sorted(ports), 'mounts': sorted(mounts), 'env': sorted(env),
    }


def _containers(run):
    """Контейнеры docker; секретные значения env скрыты."""
    argv = ['docker', 'ps', '-a', '--format', '{{.Names}}']
    code, out = run(argv)
    if code == _NOT_INSTALLED:
        return {}
    if code != 0:
        return _failed(argv, code)
    names = out.split()
    if not names:
        return {}
    argv = ['docker', 'inspect'] + names
    code, out = run(argv)
    if code != 0:
        return _failed(argv, code)
    try:
        items = json.loads(out)
    except ValueError:
        return {'_error': {'reason': 'docker inspect: вывод не разобран'}}
    return {item.get('Name', '').lstrip('/'): _container(item) for item in items}


def _wg(run):
    """WireGuard: порт и allowed-ips пиров; ключи не попадают в слепок."""
    argv = ['wg', 'show', 'all', 'dump']
    code, out = run(argv)
    if code == _NOT_INSTALLED:
        return {}
    if code != 0:
        return _failed(argv, code)
    records = {}
    for line in out.splitlines():
        fields = line.split('\t')
        if len(fields) == 5:
            records.setdefault(fields[0], {'peers': []})['listen_port'] = fields[3]
        elif len(fields) >= 5:
            records.setdefault(fields[0], {'peers': []})['peers'].append(fields[4])
    for record in records.values():
        record['peers'].sort()
    return records


def _verify(root):
    """Результаты проверок из отчёта vpn2 verify: "<component> <check>" -> result."""
    path = os.path.join(root, _VERIFY_REPORT)
    try:
        with open(path, encoding='utf-8') as stream:
            report = json.load(stream)
        return {f'{row["component"]} {row["check"]}': {'result': row['result']}
                for row in report['checks']}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError):
        return {'_error': {'reason': f'/{_VERIFY_REPORT}: отчёт не разобран'}}


def collect(root='/', run=None):
    """Слепок живого хоста: schema 1 и семь разделов.

    root - префикс чтения файлов (имена записей всегда от "/"); run(argv) -> (код, stdout).
    Раздел, чья команда вернула не 0, содержит запись "_error"; docker, wg, nft, ufw
    с кодом 127 (не установлены) дают пустой раздел.
    """
    run = run or _run
    root = str(root)
    return {
        SCHEMA_KEY: SCHEMA,
        'files': _files(root),
        'units': _units(run),
        'ports': _ports(run),
        'firewall': _firewall(run),
        'containers': _containers(run),
        'wg': _wg(run),
        'verify': _verify(root),
    }


_ERRORS = (OSError, ValueError, EOFError, ImportError, SnapshotError)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f'vpn2 snapshot: {message}\n')


def _collect_parser():
    parser = _Parser(prog='snapshot.py', description='Снять слепок хоста. Сверка: snapshot.py diff СТАРЫЙ НОВЫЙ.')
    parser.add_argument('--out', metavar='ФАЙЛ', help='записать слепок в файл (.json или .json.gz), иначе JSON в stdout')
    parser.add_argument('--baseline', metavar='ФАЙЛ', help='вычесть слепок-основу, оставить только отличия')
    parser.add_argument('--root', metavar='КАТАЛОГ', default='/', help='префикс чтения файлов (по умолчанию /)')
    return parser


def _diff_parser():
    parser = _Parser(prog='snapshot.py diff', description='Сверить два слепка; расхождения дают код 1.')
    parser.add_argument('--ignore', metavar='ФАЙЛ', help='YAML-список правил игнорирования')
    parser.add_argument('old', metavar='СТАРЫЙ', help='эталонный слепок')
    parser.add_argument('new', metavar='НОВЫЙ', help='проверяемый слепок')
    return parser


def _load_object(path):
    data = load(path)
    if not isinstance(data, dict):
        raise SnapshotError(f'{path}: слепок не объект')
    return data


def _collect_command(args):
    result = collect(args.root)
    if args.baseline:
        result = subtract(result, _load_object(args.baseline))
    if args.out:
        dump(result, args.out)
    else:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=1))
    return 0


def _diff_command(args):
    old, new = _load_object(args.old), _load_object(args.new)
    ignore = load_ignore(args.ignore) if args.ignore else []
    lines = diff(old, new, ignore)
    for rule in unused_ignore(old, new, ignore):
        print(f'ignore не сработал: {rule["section"]} {rule["name"]} {rule["field"]}', file=sys.stderr)
    if not lines:
        print('слепки совпадают')
        return 0
    for line in lines:
        print(line)
    print(f'расхождений: {len(lines)}')
    return 1


def main(argv):
    argv = list(argv)
    try:
        if argv[:1] == ['diff']:
            return _diff_command(_diff_parser().parse_args(argv[1:]))
        return _collect_command(_collect_parser().parse_args(argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    except _ERRORS as error:
        text = ' '.join(str(error).split()) or type(error).__name__
        print(f'vpn2 snapshot: {text}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
