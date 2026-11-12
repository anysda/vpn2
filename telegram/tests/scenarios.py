"""Dialogues with the bot: who does what, and what the panel and Telegram answer.

A step is (who, what): text or a command from a chat, ('press', data) for an
inline button, ('event', body[, secret]) from the panel, ('monitor', [status...])
for monitor ticks. `panel` and `tg` replace the default answers of a path; a
list is answered item by item.
"""
VIEW = {
    'name': 'Иван_П', 'deviceLimit': 3, 'expiresAt': '2026-12-31T23:59:59Z',
    'accessStatus': 'Доступ включён',
    'devices': [{'id': 1, 'name': 'Телефон', 'hasWg': True, 'configName': 'ivan-phone'},
                {'id': 2, 'name': 'Ноут_2', 'hasOvpn': True}],
    'operation': {'status': 'done', 'protocols': {
        'wg': {'state': 'confirmed'}, 'openvpn': {'state': 'not_required'},
        'ikev2': {'state': 'failed', 'error': 'ошибка_x'}}},
}
EMPTY_VIEW = {'name': 'Аня', 'devices': []}
FULL_VIEW = {**VIEW, 'deviceLimit': 2}
UNCONFIRMED_VIEW = {
    'name': 'Без лимита', 'deviceLimit': None, 'accessStatus': 'Состояние не подтверждено',
    'devices': [{'id': 3}],
    'operation': {'status': 'superseded', 'protocols': {
        'wg': {'state': 'confirmed'}, 'ikev2': {'state': 'confirmed'}}},
}
OPERATION_ID = '0a1b2c3d-0000-4000-8000-00000000abcd'
CLIENTS = [
    {'id': 'c1', 'name': 'Иван_П', 'status': 'active', 'deviceLimit': 3, 'deviceCount': 2,
     'expiresAt': '2026-12-31T23:59:59Z', 'tgLinked': True, 'accessStatus': 'Доступ включён',
     'password': 'pw-1'},
    {'id': 'c2', 'name': 'Анна', 'status': 'frozen', 'deviceLimit': None, 'deviceCount': 5,
     'expiresAt': 'garbage', 'frozenManual': True},
    {'id': 'c3', 'name': 'Пётр', 'status': 'active', 'deviceLimit': 1, 'deviceCount': 1,
     'accessStatus': 'Состояние не подтверждено',
     'operation': {'id': OPERATION_ID, 'status': 'pending', 'protocols': {
         'wg': {'state': 'failed', 'error': 'ошибка_x'}}}},
    {'id': 'c4', 'name': 'Заменён', 'status': 'active', 'deviceCount': 21,
     'accessStatus': 'Ключи перевыпущены',
     'operation': {'status': 'superseded', 'protocols': {'wg': {'state': 'confirmed'}}}},
    {},
]
STATUS = {
    'nodes': [{'tag': 'ru', 'cpu': 12.4, 'ram': 40, 'rxMbps': 1.25, 'txMbps': 0.5, 'staleSec': 3},
              {'tag': 'de', 'cpu': None, 'staleSec': 2}],
    'outbounds': [{'name': 'hy2-us-direct', 'rttMs': 120}, {'name': 'hy2-de-warp', 'rttMs': 0},
                  {'name': 'foreign-best', 'rttMs': 5}, {'name': None}],
}
NOT_LINKED = (404, {'statusMessage': 'not_linked'})
OK = (200, {})

PANEL = {
    'GET /api/bot/client?chatId=200': (200, VIEW),
    'POST /api/bot/link': (200, VIEW),
    'POST /api/bot/devices': (200, VIEW),
    'DELETE /api/bot/devices/1?chatId=200': (200, VIEW),
    'POST /api/bot/devices/1/reissue': (200, VIEW),
    'GET /api/bot/devices/1/wg-config?chatId=200': (200, '[Interface]\nPrivateKey = тест\n'),
    'GET /api/bot/devices/1/wg-qrcode.png?chatId=200': (200, b'\x89PNG\r\n\x1a\nqr'),
    'GET /api/bot/devices/2/ovpn-config?chatId=200': (200, 'client\nremote vpn 1194\n'),
    'GET /api/bot/admin/clients': (200, CLIENTS),
    **{f'GET /api/bot/admin/clients/{c["id"]}': (200, c) for c in CLIENTS[:4]},
    'POST /api/bot/admin/clients': (200, CLIENTS[0]),
    **{f'PATCH /api/bot/admin/clients/{c["id"]}': (200, c) for c in CLIENTS[:4]},
    'DELETE /api/bot/admin/clients/c1': OK,
    f'POST /api/bot/admin/access-operations/{OPERATION_ID}/retry': OK,
    'GET /api/ops/bot-snapshot': (200, STATUS),
    'POST /api/bot/events': OK,
}

CODE_409 = (409, {'statusMessage': 'device_limit_below_count'})
HANDLER_TEXT = (409, {'statusMessage': 'Conflict', 'message': 'Клиент удаляется'})
UNKNOWN_CODE = (400, {'statusMessage': 'brand_new_code'})
CRASH = RuntimeError('boom 123456:TEST_ONLY_TOKEN')
REJECTED = (200, {'ok': False, 'error_code': 400, 'description': 'query is too old'})
CANT_PARSE = (400, {'ok': False, 'error_code': 400, 'description': "Bad Request: can't parse entities"})
EVERY_CLIENT_BUTTON = [('client', ('press', data)) for data in (
    'devs', 'dev:1', 'dev:9', 'wg:1', 'wg:9', 'ovpn:2', 'ovpn:9', 'reic:1', 'reicyes:1',
    'reicyes:9', 'del:1', 'del:9', 'delyes:1', 'delyes:../admin/clients/c1', 'zzz', 'acl', '')]

CLIENT = {
    'link': {'steps': [('client', '/start secret'), ('client', '/START@vpn_test_bot secret')]},
    'link_refused': {'steps': [('client', '/start a'), ('client', '/start b'), ('client', '/start c'),
                               ('client', '/start d')],
                     'panel': {'POST /api/bot/link': [(404, {'statusMessage': 'invalid_password'}),
                                                      CODE_409, (502, 'gateway'), CRASH]}},
    'menu': {'steps': [('client', '/start'), ('client', '/help'), ('client', 'привет'),
                       ('client', '📱 Мои устройства')]},
    'menu_variants': {'steps': [('client', '/start')] * 2 + [('client', '📱 Мои устройства')] * 2,
                      'panel': {'GET /api/bot/client?chatId=200': [
                          (200, EMPTY_VIEW), (200, UNCONFIRMED_VIEW), (200, EMPTY_VIEW),
                          (200, UNCONFIRMED_VIEW)]}},
    'not_linked': {'steps': [('client', '/start'), ('client', 'привет'), ('client', '📱 Мои устройства'),
                             ('client', '➕ Добавить устройство'), ('client', ('press', 'devs')),
                             ('client', ('press', 'wg:1'))],
                   'panel': {'GET /api/bot/client?chatId=200': NOT_LINKED}},
    'not_linked_admin_named': {'steps': [('client', '/start'), ('client', ('press', 'dev:1'))],
                               'env': {'TELEGRAM_ADMIN_USERNAME': '@boss_admin'},
                               'panel': {'GET /api/bot/client?chatId=200': NOT_LINKED}},
    'panel_trouble': {'steps': [('client', '/start')] * 3 + [('client', 'привет')] * 2
                      + [('client', ('press', 'devs'))] * 3,
                      'panel': {'GET /api/bot/client?chatId=200': [
                          (500, 'secret trace'), UNKNOWN_CODE, CRASH, HANDLER_TEXT, CRASH,
                          HANDLER_TEXT, CRASH, UNKNOWN_CODE]}},
    'add_device': {'steps': [('client', '➕ Добавить устройство'), ('client', ' Ноут_3 '),
                             ('client', '➕ Добавить устройство'), ('client', '📱 Мои устройства'),
                             ('client', 'уже не имя')]},
    'add_device_refused': {'steps': [('client', '➕ Добавить устройство'), ('client', 'A')] * 4,
                           'panel': {'POST /api/bot/devices': [
                               NOT_LINKED, (409, {'statusMessage': 'device_exists'}), CRASH,
                               (404, {'statusMessage': 'not_found'})]}},
    'add_device_over_limit': {'steps': [('client', '➕ Добавить устройство'), ('client', 'имя')],
                              'panel': {'GET /api/bot/client?chatId=200': (200, FULL_VIEW)}},
    'buttons': {'steps': EVERY_CLIENT_BUTTON},
    'buttons_unlinked_mid_request': {
        'steps': [('client', ('press', data)) for data in ('wg:1', 'ovpn:2', 'reicyes:1', 'delyes:1')],
        'panel': {'GET /api/bot/devices/1/wg-qrcode.png?chatId=200': NOT_LINKED,
                  'GET /api/bot/devices/2/ovpn-config?chatId=200': NOT_LINKED,
                  'POST /api/bot/devices/1/reissue': NOT_LINKED,
                  'DELETE /api/bot/devices/1?chatId=200': NOT_LINKED}},
    'buttons_fail': {
        'steps': [('client', ('press', data)) for data in ('wg:1', 'ovpn:2', 'reicyes:1', 'delyes:1')],
        'panel': {'GET /api/bot/devices/1/wg-qrcode.png?chatId=200': (500, 'trace'),
                  'GET /api/bot/devices/2/ovpn-config?chatId=200': (409, {'statusMessage': 'ovpn_disabled'}),
                  'POST /api/bot/devices/1/reissue': CRASH,
                  'DELETE /api/bot/devices/1?chatId=200': HANDLER_TEXT},
        'tg': {'answerCallbackQuery': [(200, {'ok': True}), REJECTED, (200, {'ok': True}), REJECTED]}},
    'markdown_refused': {'steps': [('client', '/start'), ('client', ('press', 'dev:1')),
                                   ('client', ('press', 'wg:1'))],
                         'tg': {'sendMessage': [CANT_PARSE], 'editMessageText': [CANT_PARSE],
                                'sendPhoto': [CANT_PARSE], 'sendDocument': [CANT_PARSE]}},
    'edge': {'steps': [('client', '➕ Добавить устройство'), ('client', ' '), ('client', ' '),
                       ('client', ('press', 'dev:3')), ('client', ('press', 'devs'))],
             'panel': {'GET /api/bot/client?chatId=200': (200, UNCONFIRMED_VIEW)},
             'tg': {'editMessageText': [(200, {'ok': True}), (400, {
                 'ok': False, 'error_code': 400,
                 'description': 'Bad Request: message is not modified'})]}},
    'strangers': {'steps': [('client_in_group', '/start secret'), ('client_in_group', 'привет'),
                            ('client_in_group', ('press', 'wg:1')), ('client', '/status'),
                            ('client', '/nodes'), ('client', '/clients'),
                            ('client', ('press', 'adyes:c1'))]},
}


def press_all(who, *buttons):
    return [(who, ('press', data)) for data in buttons]


GROUP = {'TELEGRAM_CHAT_ID': '-100500'}
MEMBER = {name: (200, {'ok': True, 'result': {'status': name}})
          for name in ('creator', 'administrator', 'member')}
ADMIN = {
    'commands': {'steps': [('admin', '/start'), ('admin', '/help'), ('admin', '/status'),
                           ('admin', '/nodes'), ('admin', '/clients'), ('admin', 'просто текст')]},
    'commands_fail': {'steps': [('admin', '/status'), ('admin', '/status'), ('admin', '/status'),
                                ('admin', '/clients'), ('admin', '/clients')],
                      'panel': {'GET /api/ops/bot-snapshot': [
                                    (500, 'trace'), CRASH,
                                    (200, {'outbounds': [{'name': 'foreign-best', 'rttMs': 1}]})],
                                'GET /api/bot/admin/clients': [HANDLER_TEXT, CRASH]}},
    'cards': {'steps': press_all('admin', 'acl', 'acl:c1', 'acl:c2', 'acl:c3', 'acl:c4', 'zzz',
                                 'devs', 'wg:1', '')},
    'freeze': {'steps': press_all('admin', 'afz:c1', 'afz:c2', 'afz:c3'),
               'panel': {'PATCH /api/bot/admin/clients/c3': HANDLER_TEXT}},
    'retry': {'steps': press_all('admin', 'aretry:c3', 'aretry:c1', 'aretry:c4')},
    'limit': {'steps': press_all('admin', 'alm+:c1', 'alm-:c1', 'alm+:c2', 'alm-:c2', 'alm-:c4',
                                 'alm-:c3'),
              'panel': {'PATCH /api/bot/admin/clients/c3': CODE_409}},
    'expiry': {'steps': [*press_all('admin', 'aexp:c1'), ('admin', 'завтра'), ('admin', '31.12.2027'),
                         *press_all('admin', 'aexp:c2'), ('admin', ' Бессрочно '),
                         *press_all('admin', 'aexp:c3'), ('admin', '01.02.2028'), ('admin', 'мимо')],
               'panel': {'PATCH /api/bot/admin/clients/c3': UNKNOWN_CODE}},
    'new_client': {'steps': [*press_all('admin', 'anew'), ('admin', ' Новый_клиент '),
                             *press_all('admin', 'anew'), ('admin', 'Дубль'),
                             *press_all('admin', 'anew'), ('admin', 'Сбой'), ('admin', 'мимо')],
                   'panel': {'POST /api/bot/admin/clients': [
                       (200, CLIENTS[1]), (409, {'statusMessage': 'client_exists'}), CRASH]}},
    'edge': {'steps': [*press_all('admin', 'anew'), ('admin', ' '), ('admin', 'Имя'),
                       *press_all('admin', 'aexp:c1'), ('admin', 'БЕЗСРОЧНО')]},
    'invite': {'steps': press_all('admin', 'ainv:c1', 'ainv:c2')},
    'invite_without_bot_name': {'steps': press_all('admin', 'ainv:c1'), 'bot_username': None},
    'delete': {'steps': press_all('admin', 'adel:c1', 'adyes:c1', 'adel:c2', 'adyes:c2'),
               'panel': {'DELETE /api/bot/admin/clients/c2': HANDLER_TEXT}},
    'button_crash': {'steps': press_all('admin', 'acl', 'acl:c1'),
                     'panel': {'GET /api/bot/admin/clients': CRASH,
                               'GET /api/bot/admin/clients/c1': (500, 'trace')}},
    'group_chat': {
        'env': GROUP,
        'steps': [('group_admin', '/status'), ('group_admin', '/start'), ('group_member', '/status'),
                  ('group_member', '/start'), *press_all('group_member', 'adyes:c1'),
                  ('group_anonymous', '/clients'), ('group_bot', '/clients'),
                  *press_all('group_admin', 'acl'), ('group_admin', '/nodes'),
                  ('admin', '/status')],
        'tg': {'getChatMember': [MEMBER['creator'], MEMBER['administrator'], MEMBER['member'],
                                 MEMBER['member'], MEMBER['member'], MEMBER['administrator'],
                                 CRASH]}},
}

PNG = 'iVBORw0KGgpxcg=='  # base64 of a PNG signature and two bytes
EVENTS = {
    'refused': {'steps': [('intruder', {'type': 'client_created', 'name': 'x'}),
                          ('panel', 'not json'), ('panel', ['list']), ('panel', {'type': 'unknown'}),
                          ('panel', {})]},
    'queued': {'steps': [
        ('panel', {'type': 'client_created', 'name': 'Иван_П', 'deliveryId': 'd-1'}),
        ('panel', {'type': 'client_created'}),
        ('panel', {'type': 'broadcast', 'chatIds': [200, '300', 200], 'text': 'Работы *ночью*',
                   'deliveryId': 'd-2'}),
        ('panel', {'type': 'client_notify', 'chatId': '200', 'text': 'Лимит изменён'}),
        ('panel', {'type': 'system_alert', 'subject': 'backup_failed', 'detail': 'disk [full]'}),
        ('panel', {'type': 'system_alert'}),
    ]},
    'queue_refused': {'steps': [
        ('panel', {'type': 'broadcast', 'chatIds': []}),
        ('panel', {'type': 'broadcast', 'chatIds': 'all'}),
        ('panel', {'type': 'broadcast', 'chatIds': [200, True]}),
        ('panel', {'type': 'broadcast', 'chatIds': [0]}),
        ('panel', {'type': 'broadcast', 'chatIds': ['12a']}),
        ('panel', {'type': 'client_notify', 'chatId': True, 'text': 'x'}),
        ('panel', {'type': 'client_notify', 'chatId': 0, 'text': 'x'}),
        ('panel', {'type': 'client_notify', 'text': 'x'}),
        ('panel', {'type': 'client_notify', 'chatId': 200, 'text': ''}),
        ('panel', {'type': 'system_alert', 'subject': 5}),
        ('panel', {'type': 'system_alert', 'subject': 'x', 'detail': ['y']}),
    ]},
    'configs': {'steps': [
        ('panel', {'type': 'client_send_wireguard', 'name': 'ivan_phone', 'conf': '[Interface]\n',
                   'qr': PNG}),
        ('panel', {'type': 'client_send_wireguard', 'name': 'Аня', 'conf': 'c', 'qr': 'not base64!'}),
        ('panel', {'type': 'client_send_wireguard', 'name': 'Аня', 'conf': 'c', 'qr': 'cXI='}),
        ('panel', {'type': 'client_send_wireguard', 'name': 'Аня'}),
        ('panel', {'type': 'client_send_openvpn', 'name': 'ivan_phone', 'conf': 'client\n'}),
        ('panel', {'type': 'client_send_openvpn', 'conf': ''}),
        ('panel', {'type': 'client_send_ikev2', 'username': 'ivan_p', 'server': 'vpn.example',
                   'password': 'p*w', 'caCertPem': '-----BEGIN CERTIFICATE-----\n'}),
        ('panel', {'type': 'client_send_ikev2'}),
    ]},
    'invite': {'steps': [('panel', {'type': 'client_send_password', 'name': 'Иван_П',
                                    'password': 'pw-1'}),
                         ('panel', {'type': 'client_send_password', 'name': 'Иван_П'})]},
    'invite_without_bot_name': {
        'steps': [('panel', {'type': 'client_send_password', 'password': 'pw-1'})],
        'bot_username': None},
    'deploy': {'steps': [('panel', {'type': 'deploy_done'}),
                         ('panel', {'type': 'deploy_done', 'verify_unmeasured': True}),
                         ('panel', {'type': 'deploy_done', 'verify_unmeasured': 'yes'})]},
    'deploy_panel_warming_up': {
        'steps': [('panel', {'type': 'deploy_done'}),
                  ('panel', {'type': 'deploy_done', 'verify_unmeasured': True})],
        'panel': {'GET /api/ops/bot-snapshot': [(503, ''), (503, ''), (200, STATUS), *[CRASH] * 5]}},
}


def tick(**nodes):
    """One panel status: tick(ru={'cpu': 90}) is a fresh node `ru` with that load."""
    return (200, {'nodes': [{'tag': tag, 'staleSec': 1, **fields} for tag, fields in nodes.items()]})


SMALL = {'cores': 1, 'memTotalBytes': 2 * 1024 ** 3}
DOWN = (500, 'trace')
MONITOR = {
    'node_down': {'steps': [('monitor', [
        tick(ru={'staleSec': None}), tick(ru={}), tick(ru={'staleSec': 300}),
        tick(ru={'staleSec': None}), tick(ru={'staleSec': 400}), tick(ru={'staleSec': 299}),
        tick(ru={}), (200, {'nodes': [{}, {}]}), (200, {'nodes': [{}]}), (200, {})])]},
    'load': {'steps': [('monitor', [
        tick(ru={'cpu': 85, 'ram': 10}), tick(ru={'cpu': 50, 'ram': 10}),
        tick(ru={'cpu': 80, 'ram': 79.9}), tick(ru={'cpu': 99.6, 'ram': 81}),
        tick(ru={'cpu': 99, 'ram': 95}, de={'cpu': 'n/a', 'ram': None}),
        tick(ru={'cpu': 70, 'ram': 75}), tick(ru={'cpu': 69.9, 'ram': 60})])]},
    'small_node': {'steps': [('monitor', [
        *[tick(s1={'cpu': 94, 'ram': 89, **SMALL})] * 10,
        *[tick(s1={'cpu': 96, 'ram': 91, **SMALL})] * 9,
        # The size request may be late: the node keeps the thresholds of a small one.
        tick(s1={'cpu': 96, 'ram': 91}), tick(s1={'cpu': 84, 'ram': 79}),
        *[tick(big={'cpu': 96, 'ram': 91, 'cores': 4, 'memTotalBytes': 8 * 1024 ** 3})] * 2])]},
    'thresholds_from_env': {
        'env': {'ALERT_CPU_PCT': '50', 'ALERT_RAM_PCT': '60', 'ALERT_WINDOW_SEC': '90'},
        'steps': [('monitor', [*[tick(s1={'cpu': 55, 'ram': 65, **SMALL})] * 3,
                               tick(s1={'cpu': 39, 'ram': 49})])]},
    'panel_outage': {'steps': [('monitor', [*[DOWN] * 12, tick(ru={}), DOWN, tick(ru={})]),
                               ('monitor', [*[DOWN] * 5, CRASH, tick(ru={}), *[DOWN] * 11])]},
    'telegram_trouble': {
        'steps': [('monitor', [*[tick(ru={'cpu': 90})] * 3, *[DOWN] * 12, tick(ru={'cpu': 90})])],
        'tg': {'sendMessage': [REJECTED, (200, {'ok': True}), CRASH]}},
    'broken_status': {'steps': [('monitor', [(200, {'nodes': [5]}), (200, 'html'),
                                             tick(ru={'cpu': 90}), tick(ru={'cpu': 90})])]},
}

SCENARIOS = {f'{group}/{name}': scenario
             for group, scenarios in (('client', CLIENT), ('admin', ADMIN), ('events', EVENTS),
                                      ('monitor', MONITOR))
             for name, scenario in scenarios.items()}
