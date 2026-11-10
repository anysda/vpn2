"""Проверка набора бэкапа (anysda-backup.sh --check-bundle): БД среза против файлов VPN в нём.

Каждый действующий сертификат OpenVPN не отозван в CRL, у каждого действующего устройства
есть пир WireGuard и учётка IKEv2, у отключённого и удалённого их нет. Коды: 0 - расхождений
нет, 2 - каталог не набор (нет manifest.json), 3 - расхождения, 5 - сверку нечем выполнить.
"""
import base64
import calendar
import contextlib
import json
import pathlib
import re
import sqlite3
import sys
import time

from vpn2 import wg

MANIFEST = 'manifest.json'


def _tlv(data, i=0):
    tag, size = data[i], data[i + 1]
    i += 2
    if size & 0x80:
        width = size & 0x7f
        size, i = int.from_bytes(data[i:i + width], 'big'), i + width
    return tag, data[i:i + size], i + size


def _children(data):
    i = 0
    while i < len(data):
        tag, value, i = _tlv(data, i)
        yield tag, value


def _der(pem, kind):
    match = re.search(f'-----BEGIN {kind}-----(.*?)-----END {kind}-----', pem or '', re.S)
    return base64.b64decode(''.join(match.group(1).split())) if match else None


def _serial(pem):
    data = _der(pem, 'CERTIFICATE')
    if data is None:
        return None
    tbs = _tlv(_tlv(data)[1])[1]
    items = list(_children(tbs))
    return int.from_bytes(items[1 if items[0][0] == 0xa0 else 0][1], 'big')


def _timestamp(root):
    """Момент среза из манифеста; без него - сейчас."""
    try:
        return calendar.timegm(time.strptime(
            json.loads((root / MANIFEST).read_text())['timestamp'], '%Y-%m-%dT%H%M%SZ'))
    except (OSError, ValueError, KeyError, TypeError):
        return time.time()


def _problems(root, run):
    """(расхождения, что нечем проверить) по каталогу набора с db.sqlite.

    Соединение закрывается сразу: открытое чтение не даёт следующему писателю среза свернуть WAL в файл БД.
    """
    with contextlib.closing(sqlite3.connect(f'file:{root / "db.sqlite"}?mode=ro', uri=True)) as con:
        return _compare(con, root, run)


def _compare(con, root, run):

    def columns(table):
        return {row[1] for row in con.execute(f'PRAGMA table_info({table})')}

    device_columns = columns('devices')
    deleting = 'd.deleting' if 'deleting' in device_columns else '0'
    bad, nocheck = [], []
    names = dict(con.execute('SELECT id, name FROM devices')) if 'name' in device_columns else {}

    def label(device):
        """Номер и название устройства, как их ищут в панели."""
        name = ' '.join(str(names.get(device) or '').split())
        return f'устройство {device} «{name}»' if name else f'устройство {device}'

    crl = root / 'etc-openvpn/server/pki/crl.pem'
    if crl.is_file() and 'ovpn_cert' in device_columns:
        revoked = set()
        tbs = _tlv(_tlv(_der(crl.read_text(), 'X509 CRL'))[1])[1]
        for tag, value in _children(tbs):
            # Список отзыва - единственная SEQUENCE из SEQUENCE{INTEGER серийник, ...}.
            entries = [next(_children(v)) for t, v in _children(value) if t == 0x30] if tag == 0x30 else []
            if entries and len(entries) == len(list(_children(value))) and all(t == 0x02 for t, _ in entries):
                revoked |= {int.from_bytes(v, 'big') for _, v in entries}
        for device, cert in con.execute(
                f'SELECT d.id, d.ovpn_cert FROM devices d WHERE d.ovpn_cert IS NOT NULL AND {deleting} = 0'):
            if (number := _serial(cert)) in revoked:
                bad.append(f'{label(device)}: сертификат {number:X} отозван в CRL')

    # Срок считается на момент среза. Переходные устройства не сверяются: клиент с
    # незавершённой операцией доступа и срок, истекающий в пределах двух минут от среза.
    now = _timestamp(root)
    pending, retired_keys, retired_devices = set(), set(), set()
    if {'client_id', 'status', 'payload'} <= columns('access_operations'):
        for client, payload in con.execute(
                "SELECT client_id, payload FROM access_operations WHERE status = 'pending'"):
            pending.add(client)
            try:
                retired = json.loads(payload).get('retired') or []
            except (ValueError, AttributeError):
                retired = []
            retired_keys |= {old.get('wgPublicKey') for old in retired if isinstance(old, dict)}
            retired_devices |= {old.get('id') for old in retired if isinstance(old, dict)}
    joined = 'client_id' in device_columns and {'id', 'frozen_manual', 'expires_at'} <= columns('clients')

    def devices(*fields):
        """(устройство, должен быть в конфиге: True/False, None - переходное, поля)."""
        cols = ', '.join('d.' + field for field in fields)
        query = (f'SELECT d.id, {deleting}, c.frozen_manual, c.expires_at, c.id, {cols} '
                 'FROM devices d LEFT JOIN clients c ON c.id = d.client_id') if joined else (
                 f'SELECT d.id, {deleting}, 0, NULL, 0, {cols} FROM devices d')
        for device, removing, frozen, expires, client, *values in con.execute(query):
            if client in pending or (expires is not None and abs(expires - now) <= 120):
                yield device, None, values
            else:
                active = client is not None and not removing and not frozen and (expires is None or expires > now)
                yield device, bool(active and all(values)), values

    conf = root / 'etc-wireguard/wg0.conf'
    if conf.is_file() and {'wg_public_key', 'wg_preshared_key', 'wg_ip'} <= device_columns:
        # Файл, который wg отвергает, - жалоба вместо сверки; нечем спросить wg - сверка не выполнена.
        try:
            peers = {peer['key']: peer for peer in wg.conf_peers(conf.read_bytes(), run=run)}
        except wg.WgError as error:
            (nocheck if isinstance(error, wg.WgUnchecked) else bad).append(str(error))
        else:
            addresses = {key: set(peer['allowed']) for key, peer in peers.items()}
            known = set()
            for device, wanted, (key, psk, ip) in devices('wg_public_key', 'wg_preshared_key', 'wg_ip'):
                known.add(key)
                # Ключ из середины цепочки перевыпусков в retired не хранится: пир на
                # адресе переходного устройства очередь заменит сама.
                if wanted is None and ip:
                    known |= {other for other, allowed in addresses.items() if f'{ip}/32' in allowed}
                if wanted and key not in peers:
                    bad.append(f'{label(device)}: нет пира WireGuard')
                elif wanted and peers[key]['preshared_key'] != psk:
                    bad.append(f'{label(device)}: ключ пира WireGuard не совпадает с БД')
                elif wanted is False and key in peers:
                    bad.append(f'{label(device)}: пир WireGuard у отключённого устройства')
            for key in sorted(set(peers) - known - retired_keys):
                bad.append(f'пир WireGuard {key}: устройства нет в БД')

    swan = root / 'etc-swanctl/conf.d/anysda-clients.conf'
    if swan.is_file() and {'ikev2_username', 'ikev2_password', 'ikev2_ip'} <= device_columns:
        text = swan.read_text()
        conns = {int(n) for n in re.findall(r'(?m)^\s*anysda-dev-(\d+)\s*:\s*connections\.', text)}
        secrets = {int(n): (user, password) for n, user, password in
                   re.findall(r'eap-(\d+)\s*\{\s*id\s*=\s*"([^"]*)"\s*secret\s*=\s*"([^"]*)"', text)}
        known = set()
        for device, wanted, (user, password, _) in devices('ikev2_username', 'ikev2_password', 'ikev2_ip'):
            known.add(device)
            if wanted and device not in conns:
                bad.append(f'{label(device)}: нет учётки IKEv2')
            elif wanted and secrets.get(device) != (user, password):
                bad.append(f'{label(device)}: учётка IKEv2 не совпадает с БД')
            elif wanted is False and device in conns:
                bad.append(f'{label(device)}: учётка IKEv2 у отключённого устройства')
        for device in sorted(conns - known - retired_devices):
            bad.append(f'{label(device)}: учётка IKEv2 есть, устройства нет в БД')
    return bad, nocheck


def check_bundle(bundle_dir, *, run=wg.run, out=None, err=None):
    """Код выхода `anysda-backup.sh --check-bundle <каталог>`; строки расхождений - в out, отказ - в err.

    run - запуск программ для wg (как в vpn2.wg). Без db.sqlite в наборе проверять нечего - 0.
    Необработанная ошибка (битая БД, битый CRL) у bash - traceback и код 1; здесь - строка
    «Тип: текст» в err и код 1.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if not pathlib.Path(bundle_dir, MANIFEST).is_file():
        print(f"anysda-backup: --check-bundle: '{bundle_dir}' — не распакованный бандл (нет manifest.json); "
              'укажи каталог anysda-vpn2-* из архива', file=err)
        return 2
    root = pathlib.Path(bundle_dir)
    if not (root / 'db.sqlite').is_file():
        return 0
    try:
        bad, nocheck = _problems(root, run)
    except Exception as error:  # noqa: BLE001 - у bash любой сбой разбора - traceback и код 1
        print(f'{type(error).__name__}: {error}', file=err)
        return 1
    for line in bad + nocheck:
        print(line, file=out)
    return 3 if bad else 5 if nocheck else 0
