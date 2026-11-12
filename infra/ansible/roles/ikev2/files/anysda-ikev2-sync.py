#!/usr/bin/env python3
"""Счётчики байт живых IKE_SA из `swanctl --list-sas` в /etc/anysda/ikev2-status.json.

Учётные записи устройств грузит в charon служба управления; таймер только снимает счётчики.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

STATUS_FILE = '/etc/anysda/ikev2-status.json'
SA_HEAD = re.compile(r'^(\S+):\s+#(\d+),\s+(\S+?),')
EAP_ID = re.compile(r"EAP:\s+'([^']+)'")
REMOTE_ID = re.compile(r"^\s+remote\s+'([^']+)'")
# После SPI может стоять «(-|0x…)» с if_id: до числа байт идёт кусок до первой запятой.
BYTES = re.compile(r'^\s+(in|out)\s+.*?,\s*(\d+)\s+bytes')


def sessions(text):
    """[{uniqueid, username, rx, tx}]: `in` - отдача клиента (tx), `out` - его скачивание (rx)."""
    sas, current = [], None
    for line in text.splitlines():
        head = SA_HEAD.match(line)
        if head:
            current = {'uniqueid': head[2], 'username': '', 'rx': 0, 'tx': 0}
            sas.append(current)
        elif current is not None:
            eap, remote, counter = EAP_ID.search(line), REMOTE_ID.match(line), BYTES.match(line)
            if eap:
                current['username'] = eap[1]
            elif remote and not current['username']:
                current['username'] = remote[1]
            elif counter:
                current['tx' if counter[1] == 'in' else 'rx'] += int(counter[2])
    return [sa for sa in sas if sa['username']]


def main():
    try:
        text = subprocess.run(['swanctl', '--list-sas'], timeout=30, capture_output=True, text=True,
                              check=True).stdout
    except (subprocess.SubprocessError, OSError) as error:
        print(f'swanctl --list-sas: {error}', file=sys.stderr)
        return 1
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATUS_FILE))
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump({'updated': int(time.time()), 'sessions': sessions(text)}, out)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS_FILE)
    return 0


if __name__ == '__main__':
    sys.exit(main())
