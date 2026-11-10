"""Необязательная скорость Hysteria2 в Мбит/с, с точки зрения экзита,
и умолчания смены портов hy2 для config2env и генератора entry."""

import json
import os
import re
import sys

# Нет ports.hy2_hop в config.yaml - в all.env ни одной строки HY2_HOP_*,
# значения отсюда. Сторона экзита - те же числа в vpn2.exitnode.
HY2_HOP_DEFAULTS = {'enabled': '1', 'direct': '20000-24999', 'warp': '25000-29999',
                    'mgmt': '30000-30999', 'interval': '5s'}


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


def main(argv=None):
    """Фрагмент объекта для envsubst, пустой в штатном режиме BBR."""
    del argv
    try:
        fields = bandwidth_fields(os.environ.get('HY2_UP_MBPS'), os.environ.get('HY2_DOWN_MBPS'))
    except ValueError as error:
        sys.exit(f'ERROR: {error}')
    print(json.dumps(fields)[1:-1] + ',' if fields else '')
    return 0
