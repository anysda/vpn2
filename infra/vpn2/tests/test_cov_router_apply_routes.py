"""Ручные маршруты и предпочтения панели -> наборы sing-box, config.json и route-plan.json.

sing-box и systemctl подменены командами, которые пишут свои вызовы в журнал;
sing-box отвергает набор со словом REJECTME и конфиг со словом BADCONFIG.
"""
import json
import os

import pytest

from vpn2.router import apply_routes

BASE = {
    'outbounds': [{'tag': 'direct-ru'}, {'tag': 'hy2-de-direct'}, {'tag': 'hy2-de-mgmt'},
                  {'tag': 'pin-hy2-de-direct'}, {'tag': 'hy2-nl-direct'}, {'tag': 'pin-hy2-nl-direct'},
                  {'tag': 'lane-a'}],
    'route': {
        'rules': [{'inbound': ['tproxy-in'], 'action': 'sniff'},
                  {'ip_cidr': ['127.0.0.0/8'], 'action': 'reject'},
                  {'domain_suffix': ['youtube.com'], 'outbound': 'direct-ru'},
                  {'source_ip_cidr': ['10.66.66.0/24'], 'outbound': 'lane-a'}],
        'final': 'lane-a',
    },
}
TARGETS = ['direct-ru', 'pin-hy2-de-direct', 'pin-hy2-nl-direct']
SING_BOX = '''#!/bin/sh
echo "sing-box $1 $2" >> {log}
case "$1 $2" in
  "rule-set compile") grep -q REJECTME "$5" && {{ echo "набор отвергнут" >&2; exit 1; }}; exit 0 ;;
  "rule-set decompile") [ -e {root}/no-decompile ] && exit 1
     echo '{{"version": 2, "rules": [{{"ip_cidr": ["192.0.2.0/24"]}}]}}' ;;
  "check -c") grep -q BADCONFIG "$3" && {{ echo "конфиг отвергнут" >&2; exit 1; }}; exit 0 ;;
esac
'''


class Box:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.log = root / 'calls.log'
        for name in ('anysda', 'sing-box', 'bin'):
            (root / name).mkdir()
        paths = {'MANUAL_ROUTES': 'anysda/manual-routes.json', 'CLIENT_PREFS': 'anysda/client-prefs.json',
                 'SB_CONFIG': 'sing-box/config.json', 'SB_CONFIG_BASE': 'sing-box/config-base.json',
                 'RULES_DIR': 'sing-box/manual-routes', 'PREFS_DIR': 'sing-box/client-prefs',
                 'ROUTE_PLAN': 'anysda/route-plan.json', 'SB_BIN': 'bin/sing-box'}
        for name, path in paths.items():
            monkeypatch.setattr(apply_routes, name, str(root / path))
            setattr(self, name.lower(), root / path)
        self.sb_bin.write_text(SING_BOX.format(log=self.log, root=root))
        systemctl = root / 'bin/systemctl'
        systemctl.write_text(f'#!/bin/sh\necho "systemctl $*" >> {self.log}\n')
        for tool in (self.sb_bin, systemctl):
            tool.chmod(0o755)
        monkeypatch.setenv('PATH', f'{root / "bin"}{os.pathsep}{os.environ["PATH"]}')
        self.sb_config_base.write_text(json.dumps(BASE))

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def manual(self, target):
        return json.loads((self.rules_dir / f'{target}.json').read_text())['rules']

    def pref(self, node):
        return json.loads((self.prefs_dir / f'{node}.json').read_text())['rules']

    def config(self):
        return json.loads(self.sb_config.read_text())

    def plan(self):
        return json.loads(self.route_plan.read_text())


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


@pytest.fixture
def run(capsys):
    def call(*argv):
        try:
            code = apply_routes.main(list(argv))
        except SystemExit as stop:
            code = stop.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return call


def stamps(box):
    return {str(path): path.stat().st_mtime_ns for path in box.root.rglob('*.json')}


def test_no_base_and_no_config_is_a_refusal(box, run):
    box.sb_config_base.unlink()
    code, _, err = run('--no-restart')
    assert code == 1 and 'config-base.json missing' in err
    assert not box.rules_dir.exists()


def test_first_run_takes_the_live_config_as_the_base(box, run):
    box.sb_config_base.rename(box.sb_config)
    assert run('--no-restart')[0] == 0
    assert json.loads(box.sb_config_base.read_text()) == BASE


def test_empty_inputs_give_empty_sets_for_every_exit(box, run):
    code, out, err = run('--no-restart')
    assert (code, err) == (0, '')
    assert out.strip() == '0 manual route(s), 0 IP с предпочтением, наборов изменено: 5, config.json собран'
    assert sorted(os.listdir(box.rules_dir)) == sorted(f'{target}.json' for target in TARGETS)
    assert [box.manual(target) for target in TARGETS] == [[], [], []]
    assert box.pref('de') == [] and box.pref('nl') == []
    assert 'systemctl restart sing-box' not in box.calls()


def test_rules_go_after_the_service_head_and_before_the_lanes(box, run):
    run('--no-restart')
    route = box.config()['route']
    assert route['rules'][:2] == BASE['route']['rules'][:2]
    assert route['rules'][2:5] == [{'rule_set': [f'manual-{target}'], 'outbound': target} for target in TARGETS]
    assert route['rules'][5] == BASE['route']['rules'][2]
    assert route['rules'][6:8] == [{'rule_set': [f'client-pref-{node}'], 'outbound': f'pin-hy2-{node}-direct'}
                                   for node in ('de', 'nl')]
    assert route['rules'][8] == BASE['route']['rules'][3]
    assert [item['tag'] for item in route['rule_set']] == [
        *(f'manual-{target}' for target in TARGETS), 'client-pref-de', 'client-pref-nl']
    assert all(item['type'] == 'local' and item['format'] == 'source' for item in route['rule_set'])


def test_changed_config_without_vpn2_apply_is_checked_and_restarts_sing_box(box, run):
    code, out, _ = run()
    assert code == 0 and out.strip().endswith('config.json изменился, sing-box restarted')
    assert box.calls()[-2:] == ['sing-box check -c', 'systemctl restart sing-box']
    assert box.sb_config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('argv, tail', [((), 'sing-box перечитает их сам, без перезапуска'),
                                        (('--no-restart',), 'config.json уже собран')])
def test_repeat_touches_nothing(box, run, argv, tail):
    box.manual_routes.write_text(json.dumps([{'outbound': 'direct-ru', 'type': 'domain', 'value': 'example.ru'}]))
    run('--no-restart')
    before, calls = stamps(box), len(box.calls())
    code, out, err = run(*argv)
    assert (code, err) == (0, '')
    assert out.strip() == f'1 manual route(s), 0 IP с предпочтением, наборов изменено: 0, {tail}'
    assert stamps(box) == before
    assert box.calls()[calls:] == []


def test_domain_wildcard_and_subnet_land_in_the_set_of_their_exit(box, run):
    box.manual_routes.write_text(json.dumps([
        {'outbound': 'direct-ru', 'type': 'domain', 'value': 'example.ru'},
        {'outbound': 'hy2-de-direct', 'type': 'domain', 'value': '*.example.de'},
        {'outbound': 'hy2-de-direct', 'type': 'ip_cidr', 'value': '198.51.100.7/24'}]))
    code, out, err = run('--no-restart')
    assert (code, err) == (0, '') and out.startswith('3 manual route(s)')
    assert box.manual('direct-ru') == [{'domain_suffix': ['example.ru']}]
    # Маршрут на экзит идёт через pin-<выход>: сторож уводит его с мёртвого узла.
    assert box.manual('pin-hy2-de-direct') == [{'domain_suffix': ['example.de']}, {'ip_cidr': ['198.51.100.7/24']}]
    assert not (box.rules_dir / 'hy2-de-direct.json').exists()
    assert (box.rules_dir / 'direct-ru.json').stat().st_mode & 0o777 == 0o600


def test_known_outbound_outside_the_exits_gets_its_own_set(box, run):
    box.manual_routes.write_text(json.dumps([{'outbound': 'lane-a', 'type': 'domain', 'value': 'example.org'}]))
    run('--no-restart')
    assert box.manual('lane-a') == [{'domain_suffix': ['example.org']}]
    assert {'rule_set': ['manual-lane-a'], 'outbound': 'lane-a'} in box.config()['route']['rules']


def test_first_row_of_the_panel_wins_over_a_set_that_stands_above(box, run):
    box.manual_routes.write_text(json.dumps([
        {'outbound': 'hy2-nl-direct', 'type': 'domain', 'value': 'shop.example.com'},
        {'outbound': 'direct-ru', 'type': 'domain', 'value': 'example.com'}]))
    run('--no-restart')
    assert box.manual('pin-hy2-nl-direct') == [{'domain_suffix': ['shop.example.com']}]
    assert box.manual('direct-ru') == [{'type': 'logical', 'mode': 'and', 'rules': [
        {'domain_suffix': ['example.com']}, {'domain_suffix': ['shop.example.com'], 'invert': True}]}]


def test_bad_rows_are_skipped_one_by_one_and_named(box, run):
    good = {'outbound': 'direct-ru', 'type': 'domain', 'value': 'ok.example'}
    bad = [{'outbound': 'hy2-gone-direct', 'type': 'domain', 'value': 'a.example'},
           {'outbound': 'direct-ru', 'type': 'regex', 'value': '.*'},
           {'outbound': 'direct-ru', 'type': 'ip_cidr', 'value': '300.1.1.1/8'},
           {'outbound': 'direct-ru', 'type': 'domain'},
           'not-a-row', None]
    box.manual_routes.write_text(json.dumps([*bad, good]))
    code, out, err = run('--no-restart')
    assert code == 0 and out.startswith('1 manual route(s)')
    assert err.count('пропускаю маршрут') == len(bad)
    assert "outbound 'hy2-gone-direct' нет в конфиге" in err and "тип 'regex' неизвестен" in err
    assert box.manual('direct-ru') == [{'domain_suffix': ['ok.example']}]


@pytest.mark.parametrize('name, body', [('manual_routes', '[{"outbound": "direct-ru"'), ('manual_routes', '{}'),
                                        ('manual_routes', ''), ('client_prefs', '[]'), ('client_prefs', '{"de": [')])
def test_broken_input_keeps_the_rules_in_force(box, run, name, body):
    box.manual_routes.write_text(json.dumps([{'outbound': 'direct-ru', 'type': 'domain', 'value': 'kept.example'}]))
    box.client_prefs.write_text(json.dumps({'de': ['10.66.66.5']}))
    run('--no-restart')
    before = stamps(box)
    getattr(box, name).write_text(body)
    code, out, err = run('--no-restart')
    assert (code, out) == (1, '')
    assert f'{getattr(box, name)}: не читается список маршрутов' in err and 'сохраняю действующие правила' in err
    del before[str(getattr(box, name))]
    assert {path: stamp for path, stamp in stamps(box).items() if path in before} == before
    assert box.manual('direct-ru') == [{'domain_suffix': ['kept.example']}]


def test_explicit_empty_lists_clear_the_sets(box, run):
    box.manual_routes.write_text(json.dumps([{'outbound': 'direct-ru', 'type': 'domain', 'value': 'gone.example'}]))
    box.client_prefs.write_text(json.dumps({'nl': ['10.66.66.9']}))
    run('--no-restart')
    box.manual_routes.write_text('[]')
    box.client_prefs.write_text('{}')
    code, out, _ = run('--no-restart')
    assert code == 0 and 'наборов изменено: 2' in out
    assert box.manual('direct-ru') == [] and box.pref('nl') == []


def test_client_preferences_become_source_sets_of_the_node(box, run):
    box.client_prefs.write_text(json.dumps({
        'de': ['10.66.66.9', '10.66.66.5', '10.66.66.9', 'not-an-ip', 7],
        'nl': 'not-a-list', 'gone': ['10.66.66.2']}))
    code, out, err = run('--no-restart')
    assert code == 0 and '2 IP с предпочтением' in out
    assert box.pref('de') == [{'source_ip_cidr': ['10.66.66.5/32', '10.66.66.9/32']}]
    assert box.pref('nl') == []
    assert "пропускаю предпочтение 'gone': выхода hy2-gone-direct нет в конфиге" in err
    assert "пропускаю IP 'not-an-ip' предпочтения 'de'" in err and "пропускаю IP 7 предпочтения 'de'" in err


def test_sets_of_removed_exits_are_deleted(box, run):
    run('--no-restart')
    stale = [box.rules_dir / 'hy2-old-direct.json', box.prefs_dir / 'old.json']
    kept = [box.rules_dir / 'note.txt', box.prefs_dir / 'note.txt']
    for path in (*stale, *kept):
        path.write_text('{}')
    run('--no-restart')
    assert not any(path.exists() for path in stale) and all(path.exists() for path in kept)


def test_set_rejected_by_sing_box_stops_before_the_config(box, run):
    box.manual_routes.write_text(json.dumps([{'outbound': 'direct-ru', 'type': 'domain', 'value': 'ok.example'}]))
    run('--no-restart')
    config = box.sb_config.read_text()
    box.manual_routes.write_text(json.dumps([{'outbound': 'direct-ru', 'type': 'domain', 'value': 'REJECTME.example'}]))
    code, out, err = run('--no-restart')
    assert (code, out) == (1, '')
    assert f'{box.rules_dir}/direct-ru.json: проверка не прошла: набор отвергнут' in err
    assert box.manual('direct-ru') == [{'domain_suffix': ['ok.example']}]
    assert box.sb_config.read_text() == config
    assert [name for name in os.listdir(box.rules_dir) if name.endswith('.tmp')] == []


def test_config_rejected_by_sing_box_leaves_the_live_one(box, run):
    run('--no-restart')
    config, plan = box.sb_config.read_text(), box.route_plan.read_text()
    base = json.loads(box.sb_config_base.read_text())
    base['log'] = {'level': 'BADCONFIG'}
    box.sb_config_base.write_text(json.dumps(base))
    code, _, err = run()
    assert code == 1 and 'config.json: проверка не прошла: конфиг отвергнут' in err
    assert (box.sb_config.read_text(), box.route_plan.read_text()) == (config, plan)
    assert 'systemctl restart sing-box' not in box.calls()


def test_route_plan_holds_the_final_rules_and_every_set(box, run):
    base = json.loads(box.sb_config_base.read_text())
    base['route']['rule_set'] = [{'type': 'remote', 'tag': 'geoip-ru', 'format': 'binary', 'path': '/x/geoip-ru.srs'}]
    box.sb_config_base.write_text(json.dumps(base))
    box.manual_routes.write_text(json.dumps([{'outbound': 'direct-ru', 'type': 'domain', 'value': 'пример.рф'}]))
    box.client_prefs.write_text(json.dumps({'de': ['10.66.66.5']}))
    run('--no-restart')
    plan = box.plan()
    assert plan['final'] == 'lane-a' and plan['rules'] == box.config()['route']['rules']
    assert plan['rule_set']['geoip-ru'] == [{'ip_cidr': ['192.0.2.0/24']}]
    assert plan['rule_set']['manual-direct-ru'] == [{'domain_suffix': ['пример.рф']}]
    assert plan['rule_set']['client-pref-de'] == [{'source_ip_cidr': ['10.66.66.5/32']}]
    assert 'пример.рф' in box.route_plan.read_text()


def test_unreadable_binary_set_keeps_the_previous_plan(box, run):
    base = json.loads(box.sb_config_base.read_text())
    base['route']['rule_set'] = [{'type': 'remote', 'tag': 'geoip-ru', 'format': 'binary', 'path': '/x/geoip-ru.srs'}]
    box.sb_config_base.write_text(json.dumps(base))
    run('--no-restart')
    plan = box.route_plan.read_text()
    (box.root / 'no-decompile').touch()
    box.client_prefs.write_text(json.dumps({'de': ['10.66.66.5']}))
    code, _, err = run('--no-restart')
    assert code == 0 and 'набор не читается, снимок маршрутов прежний' in err
    assert box.route_plan.read_text() == plan
    assert box.pref('de') == [{'source_ip_cidr': ['10.66.66.5/32']}]
