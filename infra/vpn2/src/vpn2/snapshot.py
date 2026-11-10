"""Host snapshot comparison: baseline subtraction, ignore rules, diff, storage (stdlib only).

A snapshot is {section: {name: {field: value}}} plus the service key "schema".
"""
import fnmatch
import gzip
import io
import json
from pathlib import Path

from vpn2 import fsutil

SCHEMA_KEY = 'schema'
_IGNORE_KEYS = frozenset(('section', 'name', 'field', 'reason'))
_MIN_REASON = 10


class SnapshotError(Exception):
    """A snapshot or ignore file is malformed."""


def load_ignore(path):
    """Read a YAML list of {section, name, field, reason} ignore records.

    name is an fnmatch.fnmatchcase mask; its "*" also crosses "/", so
    /etc/wireguard/*.conf matches /etc/wireguard/sub/x.conf too.
    field is a field name or "*" (the whole record, including its presence).
    A missing or extra key, a reason shorter than 10 characters or a file that
    is not a list raises SnapshotError naming the record number (from 1).
    An empty file gives an empty list.
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
    """Keep records absent from baseline or differing from it.

    A record present in baseline and gone from snapshot stays as {"absent": True}.
    Inputs are not modified.
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
    """Record without ignored fields, or None when a "*" rule hides it entirely."""
    hidden = set()
    for rule in ignore:
        if not _matches(rule, section, name):
            continue
        if rule['field'] == '*':
            return None
        hidden.add(rule['field'])
    return {key: value for key, value in record.items() if key not in hidden}


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def diff(old, new, ignore=()):
    """Sorted list of differences between two snapshots; empty when equal.

    Lines: "<section> <name>: нет в текущем", "<section> <name>: лишнее в текущем",
    "<section> <name> <field>: <old> -> <new>" (a side without the field shows нет).
    A different schema gives the single line "schema: 1 -> 2" and nothing else.
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
    """Ignore records that match no name (and field) in old or in new."""
    unused = []
    for rule in ignore:
        used = False
        for snapshot in (old, new):
            for name, record in snapshot.get(rule['section'], {}).items():
                if not fnmatch.fnmatchcase(name, rule['name']):
                    continue
                if rule['field'] == '*' or rule['field'] in record:
                    used = True
        if not used:
            unused.append(rule)
    return unused


def dump(snapshot, path):
    """Write snapshot as sorted JSON; a .gz path is gzipped with mtime 0 (stable bytes)."""
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, indent=1).encode('utf-8')
    if str(path).endswith('.gz'):
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0) as packed:
            packed.write(raw)
        raw = buffer.getvalue()
    fsutil.atomic(path, raw)


def load(path):
    """Read a snapshot written by dump."""
    raw = Path(path).read_bytes()
    if str(path).endswith('.gz'):
        raw = gzip.decompress(raw)
    return json.loads(raw.decode('utf-8'))
