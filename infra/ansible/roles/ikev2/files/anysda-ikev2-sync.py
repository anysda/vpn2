#!/usr/bin/env python3
"""anysda-vpn2 — применение IKEv2-кредов панели и снятие счётчиков.

Раскатывается стадией 27-ikev2, гоняется таймером anysda-ikev2-sync.timer.

  1. /etc/swanctl/conf.d/anysda-clients.conf изменился (панель переписала) →
     `swanctl --load-all` (секреты, conn и пул каждого устройства) + разрыв
     SA тех устройств, чей пароль изменился или чья запись исчезла
     (удаление / заморозка / истёкший срок);
  2. каждый прогон → разрыв SA, которые сидят не на conn своего устройства
     (подключились до постоянных адресов): клиент переподключится
     и получит свой адрес;
  3. каждый прогон → счётчики байт по каждой живой SA в
     /etc/anysda/ikev2-status.json.

Снимок кредов держится в /var/anysda/ikev2-clients.state.json как
username → sha256(пароль): сам пароль на диск вне swanctl не кладём.
Отпечаток всего файла — в /var/anysda/ikev2-clients.conf.sha256.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

CLIENTS_CONF = '/etc/swanctl/conf.d/anysda-clients.conf'
STATE_FILE = '/var/anysda/ikev2-clients.state.json'
DIGEST_FILE = '/var/anysda/ikev2-clients.conf.sha256'
SHARED_CONN = 'anysda-ikev2'
RE_DEVICE_CONN = re.compile(r'^\s*(anysda-dev-\d+)\s*:', re.M)
STATUS_FILE = '/etc/anysda/ikev2-status.json'

RE_ID = re.compile(r'^\s*id\s*=\s*"?([^"]+?)"?\s*$')
RE_SECRET = re.compile(r'^\s*secret\s*=\s*"?(.*?)"?\s*$')

# Разбор `swanctl --list-sas`. Биндингов python3-vici в Ubuntu 24.04 нет,
# поэтому состояние читаем из человекочитаемого вывода:
#
#   anysda-ikev2: #7, ESTABLISHED, IKEv2, cbd5867eca6763b0_i 0cf6cdcbdda975d6_r*
#     remote '192.0.2.10' @ 198.51.100.7[1445] EAP: 'phone' [10.68.68.1]
#     net: #7, reqid 1, INSTALLED, TUNNEL, ESP:AES_GCM_16-256
#       in  c6cbc42a (-|0x0000002a),  50650 bytes,   540 packets,    11s ago
#       out ceebcf43 (-|0x0000002a), 1023795 bytes,  1100 packets,    11s ago
#
# ⚠️ После SPI может стоять «(-|0x…)» с if_id — до числа байт идёт не один
# токен, а произвольный кусок до первой запятой.
RE_SA_HEAD = re.compile(r'^(\S+):\s+#(\d+),\s+(\S+?),')
RE_EAP_ID = re.compile(r"EAP:\s+'([^']+)'")
RE_REMOTE_ID = re.compile(r"^\s+remote\s+'([^']+)'")
RE_BYTES = re.compile(r'^\s+(in|out)\s+.*?,\s*(\d+)\s+bytes')


def parse_clients(path):
    """secrets-блок swanctl → {username: sha256(secret)}."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return {}
    out, ident = {}, None
    for line in text.splitlines():
        m = RE_ID.match(line)
        if m:
            ident = m.group(1)
            continue
        m = RE_SECRET.match(line)
        if m and ident:
            out[ident] = hashlib.sha256(m.group(1).encode()).hexdigest()
            ident = None
    return out


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def live_sas():
    """[{uniqueid, username, rx, tx}] по живым IKE_SA (rx/tx — в терминах клиента).

    `in` в выводе — принято сервером от клиента, то есть отдача клиента (tx);
    `out` — отдано клиенту, то есть его скачивание (rx). Это то же соглашение,
    что у сборщика трафика панели.
    """
    try:
        out = subprocess.run(['swanctl', '--list-sas'], timeout=30,
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        print(f'swanctl --list-sas: {exc}', file=sys.stderr)
        return None

    sas, cur = [], None
    for line in out.splitlines():
        head = RE_SA_HEAD.match(line)
        if head:
            cur = {'conn': head.group(1), 'uniqueid': head.group(2),
                   'state': head.group(3), 'username': '', 'rx': 0, 'tx': 0}
            sas.append(cur)
            continue
        if cur is None:
            continue
        eap = RE_EAP_ID.search(line)
        if eap:
            cur['username'] = eap.group(1)
            continue
        remote = RE_REMOTE_ID.match(line)
        if remote and not cur['username']:
            cur['username'] = remote.group(1)
            continue
        counter = RE_BYTES.match(line)
        if counter:
            key = 'tx' if counter.group(1) == 'in' else 'rx'
            cur[key] += int(counter.group(2))
    return sas


def swanctl(*args):
    rc = subprocess.run(['swanctl', *args], stdout=subprocess.DEVNULL,
                        timeout=30).returncode
    if rc != 0:
        print(f'swanctl {" ".join(args)} rc={rc}', file=sys.stderr)
    return rc


def terminate(sa, why):
    print(f'terminate ike-id={sa["uniqueid"]} ({sa["username"]}): {why}')
    # --ike-id ждёт ЧИСЛОВОЙ uniqueid IKE_SA, не EAP-логин.
    return swanctl('--terminate', '--ike-id', sa['uniqueid']) == 0


def read_text(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def main():
    text = read_text(CLIENTS_CONF)
    digest = hashlib.sha256(text.encode()).hexdigest()
    current = parse_clients(CLIENTS_CONF)
    previous = read_json(STATE_FILE, {})
    torn = False

    if not os.path.exists('/etc/vpn2-control/api-managed') and digest != read_json(DIGEST_FILE, ''):
        # Секреты, conn и пулы устройств. Пул устройства, которое ещё сидит
        # на своём адресе, выгрузить нельзя — его догрузит повтор ниже.
        loaded = swanctl('--load-all', '--noprompt') == 0
        # Пароль сменился или устройство исчезло из secrets → рвём живую SA,
        # иначе старая сессия висит до перезагрузки (rekey_time = 0s).
        stale = {u for u, h in previous.items() if current.get(u) != h}
        if stale:
            sas = live_sas()
            if sas is None:
                return 1
            for sa in sas:
                if sa['username'] in stale and sa['uniqueid']:
                    if not terminate(sa, 'креды изменились'):
                        return 1
                    torn = True
            revoked_ids = {sa['uniqueid'] for sa in sas if sa['username'] in stale}
            remaining = live_sas()
            if remaining is None or any(sa['uniqueid'] in revoked_ids for sa in remaining):
                return 1
        # Снимок нужен для повтора отзыва: нельзя забывать старый пароль,
        # пока загрузка или разрыв SA не подтверждены charon.
        if not loaded:
            return 1
        write_json(STATE_FILE, current, 0o600)
        write_json(DIGEST_FILE, digest, 0o600)

    # Пока у устройств есть свои conn, общая conn вход не пускает: всё, что на
    # ней висит, подключилось раньше и сидит на адресе из общего пула.
    sas = live_sas()
    if sas is None:  # charon не отвечает — это авария, пусть видно в journal
        return 1
    if not os.path.exists('/etc/vpn2-control/api-managed') and RE_DEVICE_CONN.search(text):
        for sa in sas:
            # До EAP рукопожатие тоже идёт на общей conn — его не трогаем.
            if (sa['conn'] == SHARED_CONN and sa['state'] == 'ESTABLISHED'
                    and sa['uniqueid']):
                terminate(sa, 'не на своём адресе')
                torn = True
    if torn:
        swanctl('--load-pools')
        sas = live_sas()
        if sas is None:
            return 1

    try:
        connections = subprocess.run(['swanctl', '--list-conns'], timeout=30,
                                     capture_output=True, text=True, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return 1

    sessions = [{k: sa[k] for k in ('uniqueid', 'username', 'rx', 'tx')}
                for sa in sas if sa['username']]
    write_json(STATUS_FILE, {'updated': int(time.time()), 'sessions': sessions,
                            'applied_digest': read_json(DIGEST_FILE, ''),
                            'connections': re.findall(r'^(anysda-dev-\d+):', connections, re.M)}, 0o644)
    return 0


if __name__ == '__main__':
    sys.exit(main())
