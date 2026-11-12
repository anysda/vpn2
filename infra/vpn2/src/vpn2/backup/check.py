"""Проверка набора бэкапа (anysda-backup.sh --check-bundle): БД среза против файлов VPN в нём.

Каждый действующий сертификат OpenVPN не отозван в CRL, у каждого действующего устройства
есть пир WireGuard и учётка IKEv2, у отключённого и удалённого их нет. Коды: 0 - расхождений
нет, 2 - каталог не набор (нет manifest.json), 3 - расхождения, 5 - сверку нечем выполнить.
"""
import contextlib
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import time

from vpn2 import wg
from vpn2.backup.archive import MANIFEST, stamp_epoch


def _openssl(*args, data):
    done = subprocess.run(['openssl', *args], input=data, capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else ''


def _serial(pem):
    value = _openssl('x509', '-noout', '-serial', data=pem or '').strip().partition('=')[2]
    return int(value, 16) if value else None


def _revoked(crl):
    """Серийники списка отзыва; битый CRL - ValueError."""
    text = _openssl('crl', '-noout', '-text', data=crl)
    if 'Certificate Revocation List' not in text:
        raise ValueError('CRL не разобран')
    return {int(number, 16) for number in re.findall(r'Serial Number: ([0-9A-Fa-f]+)', text)}


def _timestamp(root):
    """Момент среза из манифеста; без него - сейчас."""
    try:
        return stamp_epoch(json.loads((root / MANIFEST).read_text())['timestamp'])
    except (OSError, ValueError, KeyError, TypeError):
        return time.time()


def _problems(con, root, run):
    """(расхождения, что нечем проверить) по каталогу набора с db.sqlite."""

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
        revoked = _revoked(crl.read_text())
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
    Битая БД или CRL - строка «Тип: текст» в err и код 1.
    """
    if not pathlib.Path(bundle_dir, MANIFEST).is_file():
        print(f"anysda-backup: --check-bundle: '{bundle_dir}' — не распакованный бандл (нет manifest.json); "
              'укажи каталог anysda-vpn2-* из архива', file=err or sys.stderr)
        return 2
    return compare(bundle_dir, run=run, out=out, err=err)


def compare(bundle_dir, *, run=wg.run, out=None, err=None):
    """check_bundle без требования manifest.json: срез бэкапа сверяется до записи манифеста."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    root = pathlib.Path(bundle_dir)
    if not (root / 'db.sqlite').is_file():
        return 0
    try:
        # Соединение закрывается сразу: открытое чтение не даёт следующему писателю среза свернуть WAL в файл БД.
        with contextlib.closing(sqlite3.connect(f'file:{root / "db.sqlite"}?mode=ro', uri=True)) as con:
            bad, nocheck = _problems(con, root, run)
    except Exception as error:  # noqa: BLE001 - любой сбой разбора - код 1
        print(f'{type(error).__name__}: {error}', file=err)
        return 1
    for line in bad + nocheck:
        print(line, file=out)
    return 3 if bad else 5 if nocheck else 0
