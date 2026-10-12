"""Необязательная скорость Hysteria2 в Мбит/с, с точки зрения экзита."""

import json
import os
import re
import sys


def bandwidth_fields(up, down, *, label='Hysteria2', client=False):
    """Пустая пара оставляет BBR; полная положительная пара включает Brutal."""
    values = ['' if v is None else str(v).strip() for v in (up, down)]
    if not any(values):
        return {}
    if not all(re.fullmatch(r'[0-9]+', v) and 0 < int(v) <= 1000000 for v in values):
        raise ValueError(f'{label}: нужны обе скорости up/down, целые Мбит/с от 1 до 1000000, '
                         'либо обе пустые (BBR)')
    up_mbps, down_mbps = map(int, values)
    if client:
        up_mbps, down_mbps = down_mbps, up_mbps
    return {'up_mbps': up_mbps, 'down_mbps': down_mbps}


if __name__ == '__main__':
    try:
        fields = bandwidth_fields(os.environ.get('HY2_UP_MBPS'), os.environ.get('HY2_DOWN_MBPS'))
    except ValueError as error:
        sys.exit(f'ERROR: {error}')
    # Фрагмент объекта для envsubst, пустой в штатном режиме BBR.
    print(json.dumps(fields)[1:-1] + ',' if fields else '')
