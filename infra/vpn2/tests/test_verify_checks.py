"""Список проверок verify: имена и порядок закреплены, как их печатала стадия 99."""
import json
import os

import pytest

from vpn2.verify import emergency, host, stage

BASIC = [
    'sysctl net.ipv4.ip_forward = 1',
    'ufw active',
    'fail2ban active',
    'node_exporter active',
    'sing-box active',
    'аварийка: агент vpn2-emergency active',
    'аварийка: образ установлен',
]
ENTRY = [
    'mgmt scrape: все ожидаемые targets up после свежего скрейпа',
    'ssh nl через hy2-mgmt',
    'ssh de через hy2-mgmt',
    'frontend: 127.0.0.1:80 отвечает через Caddy',
    'vmsingle: 127.0.0.1:8428 healthy',
    'tproxy: правило fwmark 0x42 → table 101',
    'tproxy: local default dev lo в table 101',
    'tproxy: wg0 цепочка и TCP/UDP правила',
    'tproxy: tun0 цепочка и TCP/UDP правила',
    'tproxy: xfrm0 цепочка и TCP/UDP правила',
    'tproxy wd: таймер active',
    'networkd: drop-in 60-anysda-foreign.conf существует',
    'yt-zapret: A/B-проверка обхода',
    'yt-failover: selector и последняя видеопроба',
    'yt-failover: таймер active',
    'wg-quick@wg0 active',
    'openvpn-server@server active',
    'strongswan-starter active',
    'anysda-ikev2-routing active',
    'ikev2: swanctl установлен, conn anysda-ikev2 загружен',
    'ikev2: charon загрузил eap-mschapv2',
    'ikev2: udp/500 слушает',
    'ikev2: udp/4500 слушает',
    'ikev2: интерфейс xfrm0 существует',
    'ikev2 sync: таймер active',
    'трафик: контрольный клиент',
    'ikev2 sync: статус обновлён менее 120 секунд назад',
    'аварийка: отправка снимка клиентов enabled и active',
]
EXIT = [
    'hy2-mgmt: udp/8444 слушает',
    'Hysteria2 direct: udp/443 слушает',
    'Hysteria2 warp: udp/8443 слушает',
    'аварийка: Hysteria2 в ожидании',
    'аварийка: выпуск сертификата на IP (caddy, vpn2-tls.timer) active',
    'аварийка: снимок клиентов от entry свежий',
]


@pytest.fixture
def node(tmp_path, monkeypatch):
    """Узел после полной установки: стадия 23 с образом прошла позже 10-foreign."""
    stamps = tmp_path / 'stamps'
    stamps.mkdir()
    (stamps / '10-foreign').write_text('')
    (stamps / '23-emergency').write_text('image 1\n')
    os.utime(stamps / '10-foreign', ns=(1, 1))
    (tmp_path / 'tls.json').write_text('{}')
    monkeypatch.setattr(stage, 'STAMPS', stamps)
    monkeypatch.setattr(stage, 'TLS_CONFIG', tmp_path / 'tls.json')
    monkeypatch.setattr(host, 'command', lambda *_argv: (0, ''))
    monkeypatch.setattr(host, 'recv_queue', lambda: '0')
    monkeypatch.setattr(emergency, 'ip_certificate', lambda _config, _cert: True)
    names = []
    monkeypatch.setattr(stage.Stage, 'check', lambda _self, label, *_args, **_options: names.append(label))
    monkeypatch.setattr(stage.Stage, 'traffic', lambda _self: names.append(stage.TRAFFIC_LABEL))
    monkeypatch.setattr(stage.Stage, 'write_report', lambda _self: None)
    return names


def test_entry_checks_in_order(node):
    env = {'HOST_TAG': 'ru', 'MGMT_IP': '10.0.0.1', 'EXIT_TAGS': 'nl de', 'YT_ROUTE': 'zapret'}
    assert stage.run(env) == 0
    assert node == BASIC + ENTRY


def test_entry_without_youtube_skips_only_its_checks(node):
    assert stage.run({'HOST_TAG': 'ru', 'MGMT_IP': '10.0.0.1', 'EXIT_TAGS': 'nl de'}) == 0
    assert node == BASIC + [name for name in ENTRY if not name.startswith('yt-')]


def test_exit_checks_in_order(node):
    assert stage.run({'HOST_TAG': 'nl', 'MGMT_IP': '10.0.0.2', 'HY2_MGMT_PORT': '8444'}) == 0
    assert node == BASIC + EXIT


def test_missing_environment_is_failure(capsys):
    assert stage.run({'HOST_TAG': 'ru'}) == 1
    assert capsys.readouterr().err == 'verify: не задано MGMT_IP\n'


@pytest.mark.parametrize('rc, output, unmeasured, result, reason, code', [
    (0, 'шум', False, 'Работает', 'проверка прошла', 0),
    (1, '', False, 'Ошибка', 'код 1', 1),
    (3, 'раз\nдва', False, 'Ошибка', 'код 3: два', 1),
    (2, 'нет пробы', True, 'Не проверено', 'нет пробы', 2),
    (2, '', True, 'Не проверено', 'команда завершилась с кодом 2', 2),
    (2, 'нет пробы', False, 'Ошибка', 'код 2', 1),
])
def test_check_result_and_code(tmp_path, rc, output, unmeasured, result, reason, code):
    current = stage.Stage({'HOST_TAG': 'ru'}, tmp_path / 'rows.jsonl')
    current.check('ikev2: проба', lambda: (rc, output), allow_unmeasured=unmeasured)
    current.check('проба узла', lambda: (0, ''))
    rows = current.read_rows()
    assert [(row['component'], row['check'], row['result'], row['reason']) for row in rows] == [
        ('ikev2', 'проба', result, reason), ('ru', 'проба узла', 'Работает', 'проверка прошла')]
    assert current.code() == code
    assert json.loads((tmp_path / 'rows.jsonl').read_text().splitlines()[0])['time']
