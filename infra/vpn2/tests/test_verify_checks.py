"""Список проверок verify: имена и порядок закреплены, как их печатала стадия 99."""
import json
import os

import pytest

from vpn2.verify import stage

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
    'nl: транспорт hy2, не запасной TCP',
    'de: транспорт hy2, не запасной TCP',
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
    monkeypatch.setattr(stage, 'SINGBOX_CONFIG', tmp_path / 'sing-box.json')
    monkeypatch.setattr(stage, 'WARP_STATE', tmp_path / 'warp.json')
    monkeypatch.setattr(stage, 'command', lambda *_argv: (0, ''))
    monkeypatch.setattr(stage, 'recv_queue', lambda: '0')
    monkeypatch.setattr(stage, 'ip_certificate', lambda _config, _cert: True)
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


def warp_config(address):
    return json.dumps({'endpoints': [{'tag': 'warp', 'type': 'wireguard', 'peers': [{'address': address}]}]})


@pytest.mark.parametrize('address, state, reason', [
    ('162.159.192.1', {'healthy_since': 5}, None),
    ('162.159.192.1', {'bad_since': 5, 'restarts': [5]}, 'Cloudflare не отвечает, сторож повторяет попытки'),
    ('127.0.0.1', {}, 'профиля Cloudflare нет, регистрация не прошла'),
])
def test_exit_warns_about_dead_warp(node, tmp_path, capsys, address, state, reason):
    (tmp_path / 'sing-box.json').write_text(warp_config(address))
    (tmp_path / 'warp.json').write_text(json.dumps(state))
    assert stage.run({'HOST_TAG': 'nl', 'MGMT_IP': '10.0.0.2', 'HY2_MGMT_PORT': '8444'}) == 0
    assert node == BASIC + EXIT
    warnings = [line for line in capsys.readouterr().err.splitlines() if '⚠ WARP' in line]
    expected = [f'[nl] ⚠ WARP: {reason} — экзит работает без канала warp; повтор: ./deploy.sh 10-foreign nl']
    assert warnings == (expected if reason else [])


def test_warp_state_unreadable_is_not_a_warning(tmp_path):
    (tmp_path / 'c').write_text(warp_config('162.159.192.1'))
    assert stage.warp_trouble(tmp_path / 'c', tmp_path / 'none') is None
    assert stage.warp_trouble(tmp_path / 'none', tmp_path / 'none') is None


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


class Proxies:
    def __init__(self, answer):
        self.answer = answer

    def proxies(self, *, timeout):
        assert timeout
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize('answer, rc, reason', [
    ({'transport-nl-direct': {'now': 'transport-hy2-nl-direct'}, 'hy2-nl-mgmt': {'now': 'transport-hy2-nl-mgmt'}}, 0, ''),
    ({'transport-nl-direct': {'now': 'transport-tcp-nl-direct'}, 'transport-nl-warp': {'now': 'transport-hy2-nl-warp'},
      'hy2-nl-mgmt': {'now': 'transport-tcp-nl-mgmt'}}, 1,
     'hy2 до экзита nl не работает, трафик на запасном TCP (VLESS Reality): transport-nl-direct, hy2-nl-mgmt'),
    ({'hy2-nl-mgmt': {'type': 'Hysteria2'}}, 0, ''),
    (ConnectionRefusedError('refused'), 2, 'clash-api sing-box не ответил: refused'),
], ids=['hy2', 'tcp-fallback', 'without-fallback', 'api-down'])
def test_exit_transport_reports_tcp_fallback(monkeypatch, answer, rc, reason):
    # Маршруты на VLESS-TCP проходят: закрытый hy2 видно только по селекторам.
    monkeypatch.setattr(stage.ClashApi, 'from_config', classmethod(lambda _cls, _path: Proxies(answer)))
    assert stage.exit_transport('nl') == (rc, reason)
