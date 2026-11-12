"""Отзывы после архива при restore: отзывы живого узла переносятся в распакованный набор.

Старый архив помнит ключи, которые узел с тех пор отозвал; отзывы живого узла
переносятся в распакованный набор до замены (openssl ca -revoke/-gencrl,
правка wg0.conf и swanctl набора, список «выдано после архива»).
"""
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile

from vpn2 import wg
from vpn2.backup.archive import ArchiveError as RevokeError

HEX = re.compile(r'[0-9A-Fa-f]{1,64}')
DEV_CN = re.compile(r'(?:^|[/,])CN=dev_(\d+)(?:[/,]|$)')


def say(message):
    print('anysda-restore: ' + message, flush=True)


def read(path, newline=None, encoding=None):
    """Текст файла или None, если его нет или он не читается."""
    try:
        with open(path, encoding=encoding, errors='replace', newline=newline) as f:
            return f.read()
    except OSError:
        return None


def digest(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read().strip()).hexdigest()
    except OSError:
        return None


def wg_key(directory):
    """sha256 приватного ключа сервера WireGuard: keys/, server.priv или [Interface] из wg0.conf."""
    found = digest(os.path.join(directory, 'keys/server.priv')) or digest(os.path.join(directory, 'server.priv'))
    if found:
        return found
    key = wg.private_key(read(os.path.join(directory, 'wg0.conf'), newline='', encoding='latin-1') or '')
    return hashlib.sha256(key.encode()).hexdigest() if key else None


def access_kinds(wg_public_key, ovpn_cert, ikev2_password):
    return {kind for kind, value in (('WireGuard', wg_public_key), ('OpenVPN', ovpn_cert),
                                     ('IKEv2', ikev2_password)) if value}


def wg_public_keys(text):
    """PublicKey всех пиров wg0.conf так, как их читает wg."""
    pairs = (wg.line(line.replace('\0', '')) for line in text.split('\n'))
    return {value for name, value in pairs if name == 'PublicKey' and value}


def index_rows(path):
    return [line.split('\t') for line in (read(path) or '').splitlines()]


def devices(path, writable=False):
    """(соединение, строки devices по id, столбцы, последний выданный номер); без столбца id - (None, ...)."""
    con = sqlite3.connect(f'file:{path}' + ('' if writable else '?mode=ro'), uri=True)
    columns = {row[1] for row in con.execute('PRAGMA table_info(devices)')}
    if 'id' not in columns:
        con.close()
        return None, {}, set(), 0
    con.row_factory = sqlite3.Row
    rows = {row['id']: dict(row) for row in con.execute('SELECT * FROM devices')}
    try:
        seq = con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'devices'").fetchone()
    except sqlite3.Error:
        seq = None
    return con, rows, columns, max([seq[0] if seq else 0, *rows])


def run(bundle, issued_list, *, live_db='/var/lib/anysda-vpn2/db.sqlite',
        live_pki='/etc/openvpn/server/pki', live_wg_dir='/etc/wireguard',
        live_ike_conf='/etc/swanctl/conf.d/anysda-clients.conf',
        live_ccd='/etc/openvpn/server/ccd', runner=subprocess.run):
    """Перенос отзывов в набор; возвращает код выхода, ошибка openssl - RevokeError."""
    pki = os.path.join(bundle, 'etc-openvpn/server/pki')
    db_path = os.path.join(bundle, 'db.sqlite')
    wg_path = os.path.join(bundle, 'etc-wireguard/wg0.conf')
    ike_path = os.path.join(bundle, 'etc-swanctl/conf.d/anysda-clients.conf')

    def serial_of(pem):
        out = runner(['openssl', 'x509', '-noout', '-serial'], input=pem, text=True,
                     capture_output=True, check=False).stdout
        value = out.strip().partition('=')[2]
        return int(value, 16) if HEX.fullmatch(value) else None

    live_ca = digest(os.path.join(live_pki, 'ca.crt'))
    archive_ca = digest(os.path.join(pki, 'ca.crt'))
    live_wg = wg_key(live_wg_dir)
    archive_wg = wg_key(os.path.join(bundle, 'etc-wireguard'))
    if not (live_ca and live_ca == archive_ca) and not (live_wg and live_wg == archive_wg):
        reason = 'на узле нет прежних ключей' if not (live_ca or live_wg) else 'архив другой установки'
        say(f'отозванных записей применено поверх архива: 0 — {reason}, отзывы узла к нему не относятся')
        return 0
    same_ca = bool(live_ca) and live_ca == archive_ca

    # Живой index: отозванные строки и сертификаты dev_N (серийник -> (номер, действует)).
    live_revoked, indexed = {}, {}
    for row in index_rows(os.path.join(live_pki, 'index.txt')) if same_ca else ():
        if len(row) != 6 or not HEX.fullmatch(row[3]):
            continue
        if row[0] == 'R':
            live_revoked[int(row[3], 16)] = row
        if found := DEV_CN.search(row[5]):
            indexed[int(row[3], 16)] = (int(found[1]), row[0] == 'V')
    # certificateHold ставит только restore — выданным после архива. Устройство,
    # чей сертификат в восстанавливаемом архиве, получает доступ обратно.
    held = {serial for serial, row in live_revoked.items() if row[2].partition(',')[2] == 'certificateHold'}

    live, live_seq, live_read = {}, 0, False
    if os.path.isfile(live_db):
        try:
            con, live, _columns, live_seq = devices(live_db)
            if con:
                con.close()
                live_read = True
        except sqlite3.Error as error:
            say(f'WARN: живая БД не читается ({error}) — переношу только отзывы сертификатов')
            live, live_seq = {}, 0

    # Без живой БД живые конфиги узла судят только устройства без сертификата
    # OpenVPN в архиве, а у отозванных панелью подсказывают подпись в журнале.
    live_peers = live_ike = None
    if not live_read:
        live_peers = read(os.path.join(live_wg_dir, 'wg0.conf'), newline='', encoding='latin-1')
        live_ike = read(live_ike_conf)
    if live_peers is not None:
        live_peers = wg_public_keys(live_peers)
    if live_ike is not None:
        secrets = set()
        for block in re.findall(r'(?ms)^\s*eap-\d+\s*\{(.*?)\}', live_ike):
            name = re.search(r'(?m)^\s*id\s*=\s*"([^"]*)"', block)
            secret = re.search(r'(?m)^\s*secret\s*=\s*"([^"]*)"', block)
            secrets.add((name and name[1], secret and secret[1]))
        live_ike = secrets

    # Устройство архива против живого: удалено (сертификат отозван или номер
    # уже выдан и строки нет) или перевыпущено (другой ключ, сертификат, пароль).
    revoke = {}
    unproven = set()
    issued = {}
    reissued = {}
    by_cert = set()
    released = {}
    archive_db = None
    archive_seq = seq_floor = 0
    absent_ids = set()
    if os.path.isfile(db_path):
        archive_db, archived, columns, archive_seq = devices(db_path, writable=True)
        archive_serials = set()
        for device_id, row in archived.items():
            mine = live.get(device_id)
            cert = row.get('ovpn_cert')
            serial = serial_of(cert) if cert and same_ca else None
            archive_serials.add(serial)
            if serial in held:
                released[serial] = device_id
            # Номер мог достаться другому устройству после прежнего restore. Сертификат
            # отозван не restore (не hold) — панель удалила это устройство до того.
            if mine and row.get('created_at') and mine.get('created_at') and row['created_at'] != mine['created_at']:
                if serial not in live_revoked or serial in held:
                    continue
                mine = None
            # Отозвано прежним restore, а не удалено: строки нет, номер не выше выданных.
            if mine is None and serial in held:
                continue
            what = set()
            if not live_read:
                user, password = row.get('ikev2_username'), row.get('ikev2_password')
                had = access_kinds(row.get('wg_public_key'), cert, password)
                # Сертификат OpenVPN отзывают только удаление и перевыпуск в панели, и оба
                # снимают все виды доступа; заморозка и срок пишут disable в ccd. Отозван —
                # снято всё, живые конфиги лишь подсказывают подпись: учётка IKEv2 на узле
                # та же — перевыпуск, её нет — удаление, конфига нет — не знаем.
                # Действует — всё из архива.
                if serial is not None:
                    if serial in live_revoked and serial not in held:
                        gone = None if live_ike is None else not (user and any(name == user for name, _ in live_ike))
                        revoke[device_id] = (had, gone, row)
                        by_cert.add(device_id)
                    continue
                # Без сертификата заморозку от удаления не отличить: каждый вид доступа
                # по живым конфигам, перевыпуск в панели вернёт снятое.
                if not cert:
                    unproven.add(device_id)
                if row.get('wg_public_key') and live_peers is not None and row['wg_public_key'] not in live_peers:
                    what.add('WireGuard')
                if password and live_ike is not None and not any(
                        secret == password and (not user or name == user) for name, secret in live_ike):
                    what.add('IKEv2')
                gone = bool(what) and what == had and not (user and live_ike and any(name == user for name, _ in live_ike))
                if what:
                    revoke[device_id] = (what, gone, row)
                continue
            gone = mine is None and (serial in live_revoked or 0 < device_id <= live_seq)
            if mine is None and not gone:
                continue
            if row.get('wg_public_key') and (gone or row['wg_public_key'] != mine.get('wg_public_key')):
                what.add('WireGuard')
            if cert and (gone or serial in live_revoked or cert != mine.get('ovpn_cert')):
                what.add('OpenVPN')
            if row.get('ikev2_password') and (gone or (row['ikev2_password'], row.get('ikev2_username'))
                                              != (mine.get('ikev2_password'), mine.get('ikev2_username'))):
                what.add('IKEv2')
            if what:
                revoke[device_id] = (what, gone, row)
        # Обратное множество: устройство есть в живой БД, но не в архиве (или под
        # его номером в архиве другое устройство) — выдано после среза. В
        # восстановленной БД его нет, а сертификат подписан тем же CA: без отзыва
        # он подключается, и его ccd с адресом достанется следующему с тем же id.
        for device_id, row in (live.items() if archive_db else ()):
            mine = archived.get(device_id)
            if mine and not (row.get('created_at') and mine.get('created_at')
                             and row['created_at'] != mine.get('created_at')):
                continue
            what = access_kinds(row.get('wg_public_key'), same_ca and row.get('ovpn_cert'), row.get('ikev2_password'))
            issued[device_id] = (what, mine is None, row)
        absent_ids = {device_id for device_id, (_w, absent, _r) in issued.items() if absent}
        # Живая БД могла пропасть или не читаться, а PKI и ccd узла переживают её,
        # и OpenVPN проверяет именно их. Выданное после архива — и действующий
        # сертификат dev_N, которого нет у устройств архива, и ccd номера вне архива.
        # Хвосты newcerts без строки в index остались от прежних restore.
        pki_certs, pki_ids = {}, set()
        if same_ca:
            newcerts = os.path.join(live_pki, 'newcerts')
            for name in sorted(os.listdir(newcerts) if os.path.isdir(newcerts) else ()):
                pem = read(os.path.join(newcerts, name))
                if pem is None:
                    continue
                serial = serial_of(pem)
                if serial in indexed:
                    device_id, valid = indexed[serial]
                else:
                    subject = runner(['openssl', 'x509', '-noout', '-subject', '-nameopt', 'RFC2253'],
                                             input=pem, text=True, capture_output=True, check=False).stdout
                    found = DEV_CN.search(subject.strip().partition('=')[2])
                    if not found:
                        continue
                    device_id, valid = int(found[1]), True
                pki_ids.add(device_id)
                if valid and serial not in archive_serials:
                    pki_certs[serial] = (device_id, pem)
            pki_ids.update(device_id for device_id, _valid in indexed.values())
        ccd_ids = {int(m[1]) for m in (re.fullmatch(r'dev_(\d+)', name)
                                       for name in (os.listdir(live_ccd) if os.path.isdir(live_ccd) else ())) if m}
        absent_ids |= {n for n in pki_ids | ccd_ids if n not in archived}
        # Учётки IKEv2 нет и после прежнего restore, обнулившего креды перевыпущенного.
        # Удаление в панели оставляет ccd и пишет в него disable, у перевыпущенного
        # ccd без disable; ccd нет или не читается — не знаем. Номер удалённого занят
        # действующим сертификатом dev_N — это другое устройство: перевыпуск оставил
        # бы учётку, его отзыв обратим, как у выданных после архива. Но учётку с узла
        # снимают и заморозка, и срок перевыпущенного: при disable и действующем dev_N
        # не знаем, перевыпуск это без отзыва — dev_N отзывается насовсем, как перевыпуск.
        taken = {device_id for device_id, _pem in pki_certs.values()}
        replaced = set()
        for device_id in by_cert:
            what, gone, row = revoke[device_id]
            if not gone:
                continue
            text = read(os.path.join(live_ccd, f'dev_{device_id}'))
            disabled = None if text is None else any(line.strip() == 'disable' for line in text.splitlines())
            if device_id in taken and not disabled:
                replaced.add(device_id)
                continue
            revoke[device_id] = (what, None if device_id in taken else disabled, row)
        seq_floor = max([live_seq, *pki_ids, *ccd_ids])
        known = {serial_of(row['ovpn_cert']) for what, _a, row in issued.values() if 'OpenVPN' in what}
        for serial, (device_id, pem) in pki_certs.items():
            if serial in known:
                continue
            # Перевыпуск: устройство остаётся в восстановленной БД с обнулёнными кредами,
            # панель этот сертификат уже не отзовёт. Отзыв без причины, как панель: hold
            # вернул бы доступ следующим restore, в том числе удалённому после него.
            if device_id in archived and device_id not in issued and device_id not in replaced:
                reissued.setdefault(device_id, []).append(pem)
                continue
            what, absent, row = issued.get(device_id, (set(), device_id not in archived, {}))
            issued[device_id] = (what | {'OpenVPN'}, absent, {**row, 'pki_certs': [*row.get('pki_certs', ()), pem]})

    wg_keys = {row['wg_public_key'] for what, _gone, row in revoke.values() if 'WireGuard' in what}
    ike_ids = {device_id for device_id, (what, _gone, _row) in revoke.items() if 'IKEv2' in what}
    ike_secrets = {row['ikev2_password'] for what, _gone, row in revoke.values() if 'IKEv2' in what}
    certs = [(row['ovpn_cert'], None) for what, _gone, row in revoke.values() if 'OpenVPN' in what]
    certs.extend((pem, None) for pems in reissued.values() for pem in pems)
    # У выданных после архива строк в восстановленной БД нет, чистить нечего.
    # Номер учётки IKEv2 снимаем только если в архиве под ним нет другого устройства.
    for device_id, (what, absent, row) in issued.items():
        if 'WireGuard' in what:
            wg_keys.add(row['wg_public_key'])
        if 'IKEv2' in what:
            ike_secrets.add(row['ikev2_password'])
            if absent:
                ike_ids.add(device_id)
        if 'OpenVPN' in what:
            certs.extend((cert, 'certificateHold') for cert in (row.get('ovpn_cert'), *row.get('pki_certs', ())) if cert)
    with open(issued_list, 'w') as f:
        f.write(''.join(f'{device_id}\n' for device_id in sorted(absent_ids)))

    if archive_db:
        fields = {'WireGuard': ('wg_private_key', 'wg_public_key', 'wg_preshared_key'),
                  'OpenVPN': ('ovpn_cert', 'ovpn_key'), 'IKEv2': ('ikev2_password',)}
        with archive_db:
            for device_id, (what, _gone, _row) in revoke.items():
                cleared = [c for name in sorted(what) for c in fields[name] if c in columns]
                archive_db.execute(f'UPDATE devices SET {", ".join(c + " = NULL" for c in cleared)} WHERE id = ?',
                                   (device_id,))
            # Номера, выданные узлом после среза (и уже удалённые), не выдаются повторно.
            has_seq = archive_db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'").fetchone()
            if has_seq and seq_floor > archive_seq:
                if not archive_db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'devices'",
                                          (seq_floor,)).rowcount:
                    archive_db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('devices', ?)", (seq_floor,))
        archive_db.close()

    def rewrite(path, text, encoding=None):
        # Владелец и режим файла архива остаются прежними; latin-1 с newline='' - байт в байт.
        with open(path, 'w', encoding=encoding, newline='' if encoding else None) as f:
            f.write(text)

    if wg_keys and os.path.isfile(wg_path):
        # Секции и ключи как у wg-quick и wg: строка до `\n`, NUL вон, до первого `#`, без
        # пробельных символов, имена в любом регистре ASCII. Оставшиеся строки пишутся как в файле, байт в байт.
        # Строки-комментарии вплотную над секцией - её подпись и уходят с ней; пустые строки и комментарии
        # в конце файла остаются на месте.
        def detach_notes(block):
            n = len(block)
            while n and not block[n - 1][0] and '#' in block[n - 1][1]:
                n -= 1
            notes = block[n:]
            del block[n:]
            return notes

        blocks = [[]]
        for line in re.findall(r'[^\n]*\n|[^\n]+', open(wg_path, encoding='latin-1', newline='').read()):
            clean = re.sub(wg.BLANK, '', line.replace('\0', '').rstrip('\n').split('#', 1)[0])
            if clean.startswith('['):
                blocks.append(detach_notes(blocks[-1]))
            blocks[-1].append((clean, line))
        blocks.append(detach_notes(blocks[-1]))
        kept = []
        for block in blocks:
            keys = [clean.partition('=')[2] for clean, _ in block if clean.lower().startswith('publickey=')]
            names = [clean.lower() for clean, _ in block if clean]
            if names and names[0] == '[peer]' and keys and keys[-1] in wg_keys:
                continue
            kept += [line for _, line in block]
        rewrite(wg_path, ''.join(kept), 'latin-1')

    if (ike_ids or ike_secrets) and os.path.isfile(ike_path):
        out, block, depth = [], None, 0
        for line in open(ike_path).read().split('\n'):
            opens, closes = line.count('{'), line.count('}')
            if block is None and depth == 1 and opens:
                block = [line]
            elif block is not None:
                block.append(line)
            else:
                out.append(line)
            depth += opens - closes
            if block is not None and depth == 1:
                text = '\n'.join(block)
                owner = re.match(r'\s*(?:anysda-dev-(\d+)\s*:|eap-(\d+)\s*\{)', block[0])
                secret = re.search(r'(?m)^\s*secret\s*=\s*"([^"]*)"', text)
                if not ((owner and int(owner[1] or owner[2]) in ike_ids) or (secret and secret[1] in ike_secrets)):
                    out.extend(block)
                block = None
        rewrite(ike_path, '\n'.join(out))

    revoked_certs = 0
    index_path = os.path.join(pki, 'index.txt')
    if same_ca and os.path.isfile(index_path):
        rows = index_rows(index_path)
        position = {int(r[3], 16): n for n, r in enumerate(rows) if len(r) == 6 and HEX.fullmatch(r[3])}
        newcerts = os.path.join(pki, 'newcerts')
        for serial, row in sorted(live_revoked.items()):
            if serial in released:
                continue
            if serial in position:
                if rows[position[serial]][0] == 'R':
                    continue
                rows[position[serial]] = row
            else:
                rows.append(row)
                source = os.path.join(live_pki, 'newcerts', row[3] + '.pem')
                target = os.path.join(newcerts, row[3] + '.pem')
                if os.path.isfile(source) and os.path.isdir(newcerts) and not os.path.exists(target):
                    shutil.copy2(source, target)
                    stat = os.stat(newcerts)
                    os.chown(target, stat.st_uid, stat.st_gid)
            revoked_certs += 1
        rewrite(index_path, ''.join('\t'.join(r) + '\n' for r in rows))
        config = open(os.path.join(pki, 'openssl.cnf') if os.path.isfile(os.path.join(pki, 'openssl.cnf'))
                      else os.path.join(live_pki, 'openssl.cnf')).read()
        with tempfile.TemporaryDirectory(dir=pki) as work:
            cnf = os.path.join(work, 'restore.cnf')
            with open(cnf, 'w') as f:
                f.write(re.sub(r'(?m)^dir\s*=.*$', lambda _m: 'dir = ' + pki, config))
            revoked = {int(r[3], 16) for r in index_rows(index_path) if len(r) == 6 and r[0] == 'R'}
            for cert, reason in certs:
                if serial_of(cert) in revoked:
                    continue
                pem = os.path.join(work, 'revoke.pem')
                with open(pem, 'w') as f:
                    f.write(cert)
                # Чужой или просроченный сертификат и так не пройдёт проверку OpenVPN.
                if runner(['openssl', 'verify', '-CAfile', os.path.join(pki, 'ca.crt'), pem],
                                  capture_output=True, check=False).returncode:
                    continue
                done = runner(['openssl', 'ca', '-batch', '-config', cnf, '-revoke', pem,
                                       *(['-crl_reason', reason] if reason else [])],
                                      capture_output=True, text=True, check=False)
                if done.returncode:
                    raise RevokeError(1, 'openssl ca -revoke: ' + (done.stderr.strip().splitlines() or ['?'])[-1])
                revoked.add(serial_of(cert))
                revoked_certs += 1
            # Номера не должны повториться: следующий сертификат и CRL — после живых.
            for name in ('serial', 'crlnumber'):
                values = []
                for folder in (pki, live_pki):
                    value = (read(os.path.join(folder, name)) or '').strip()
                    if HEX.fullmatch(value):
                        values.append(int(value, 16))
                if values and os.path.isfile(os.path.join(pki, name)):
                    text = format(max(values), 'X')
                    rewrite(os.path.join(pki, name), text.zfill(len(text) + len(text) % 2) + '\n')
            if revoked_certs or released:
                crl = os.path.join(work, 'crl.pem')
                runner(['openssl', 'ca', '-batch', '-config', cnf, '-gencrl', '-out', crl],
                               check=True, capture_output=True)
                runner(['openssl', 'crl', '-in', crl, '-noout', '-verify', '-CAfile',
                                os.path.join(pki, 'ca.crt')], check=True, capture_output=True)
                rewrite(os.path.join(pki, 'crl.pem'), open(crl).read())

    wg_count, ike_count = len(wg_keys), len(ike_ids)
    say(f'отозванных записей применено поверх архива: {wg_count + revoked_certs + ike_count} '
        f'(WireGuard {wg_count}, OpenVPN {revoked_certs}, IKEv2 {ike_count})')
    for device_id, (what, gone, _row) in sorted(revoke.items()):
        state = ('отозвано после архива — удалено или перевыпущено в панели, проверь его там' if gone is None else
                 'удалено после архива — удали его в панели' if gone else 'перевыпущено после архива — перевыпусти в панели')
        say(f'  устройство {device_id}: {", ".join(sorted(what))} отозваны; {state}')
        if device_id in unproven:
            say('    сертификата OpenVPN в архиве нет — судил по живым конфигам, заморозку от удаления не отличить')
    for device_id in sorted(set(reissued) - set(revoke)):
        say(f'  устройство {device_id}: OpenVPN отозваны; перевыпущено после архива — перевыпусти в панели')
    for device_id, (what, _absent, _row) in sorted(issued.items()):
        say(f'  устройство {device_id}: {", ".join(sorted(what)) or "доступов нет"} отозваны; '
            'выдано после архива — в восстановленной базе его нет, доступ вернёт restore архива, где оно есть')
    if released:
        say('отозванным прежним restore доступ возвращён — они есть в архиве: '
            + ', '.join(f'№{n}' for n in sorted(released.values())))

    return 0
