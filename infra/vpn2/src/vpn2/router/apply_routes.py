"""Ручные маршруты и предпочтения панели -> config.json sing-box и route-plan.json.

`vpn2 router apply-routes [--no-restart]`; --no-restart только собирает
config.json (перезапуск sing-box делает установка, один раз на итоговом конфиге).
"""
import ipaddress
import json
import os
import subprocess
import sys
import tempfile

MANUAL_ROUTES = '/etc/anysda/manual-routes.json'
CLIENT_PREFS  = '/etc/anysda/client-prefs.json'
SB_CONFIG     = '/etc/sing-box/config.json'
SB_CONFIG_BASE= '/etc/sing-box/config-base.json'
RULES_DIR     = '/etc/sing-box/manual-routes'
PREFS_DIR     = '/etc/sing-box/client-prefs'
ROUTE_PLAN    = '/etc/anysda/route-plan.json'
SB_BIN        = '/usr/local/bin/sing-box'


def main(argv=None):
    NO_RESTART = '--no-restart' in (sys.argv[1:] if argv is None else argv)

    if not os.path.exists(SB_CONFIG_BASE) and os.path.exists(SB_CONFIG):
        import shutil
        shutil.copy2(SB_CONFIG, SB_CONFIG_BASE)

    if not os.path.exists(SB_CONFIG_BASE):
        print('config-base.json missing', file=sys.stderr); sys.exit(1)

    def load_input(path, expected):
        try:
            with open(path) as f:
                value = json.load(f)
            if not isinstance(value, expected):
                raise ValueError(f'ожидался {expected.__name__}')
            return value
        except FileNotFoundError:
            return expected()
        except (ValueError, OSError) as e:
            print(f'{path}: не читается список маршрутов: {e}; сохраняю действующие правила', file=sys.stderr)
            sys.exit(1)

    # Читаем оба входа до записи любых наборов. Повреждённый/недописанный JSON
    # не означает удаление правил: только явные [] и {} очищают списки.
    rows = load_input(MANUAL_ROUTES, list)
    prefs = load_input(CLIENT_PREFS, dict)

    with open(SB_CONFIG_BASE) as f: cfg = json.load(f)

    # Правило на outbound, которого нет в конфиге (экзит убрали, опечатка в
    # панели), sing-box отвергает целиком — и вместе с ним все остальные ручные
    # маршруты. Такие строки пропускаем по одной и пишем в журнал.
    known = {o.get('tag') for o in cfg.get('outbounds', []) + cfg.get('endpoints', [])}
    manual = []
    for row in rows if isinstance(rows, list) else []:
        try:
            out, kind, val = row['outbound'], row['type'], str(row['value'])
            if out not in known:
                raise ValueError(f'outbound {out!r} нет в конфиге')
            # Ручной маршрут на экзит — через pin-<выход>: вотчдог переводит его на
            # foreign-best, пока узел выключен или выход мёртв (иначе маршрут рвётся).
            if f'pin-{out}' in known:
                out = f'pin-{out}'
            if kind == 'domain':
                rule = {'domain_suffix': [val[2:] if val.startswith('*.') else val]}
            elif kind == 'ip_cidr':
                ipaddress.ip_network(val, strict=False)
                rule = {'ip_cidr': [val]}
            else:
                raise ValueError(f'тип {kind!r} неизвестен')
        except (KeyError, TypeError, ValueError) as e:
            print(f'пропускаю маршрут {row!r}: {e}', file=sys.stderr)
            continue
        manual.append((out, rule))

    # Маршруты лежат в local rule-set'ах, по набору на выход, а config.json на них
    # только ссылается. sing-box сам перечитывает изменённый файл набора и
    # подменяет правила на ходу, соединения не рвутся; SIGHUP и рестарт рвут все.
    # Наборы заведены на каждый выход, который предлагает панель, даже пустые:
    # тогда правка маршрута меняет только файлы, а config.json остаётся прежним.
    targets = []
    for o in cfg.get('outbounds', []):
        t = o.get('tag', '')
        if t == 'direct-ru' or (t.startswith('hy2-') and not t.endswith('-mgmt')):
            targets.append(f'pin-{t}' if f'pin-{t}' in known else t)
    for out, _ in manual:
        if out not in targets:
            targets.append(out)
    pos = {t: i for i, t in enumerate(targets)}

    # Побеждает первая строка панели. Наборы же стоят в порядке выходов, поэтому
    # из строки вычитаем более ранние строки тех наборов, что стоят ниже её.
    sets = {t: [] for t in targets}
    for i, (out, rule) in enumerate(manual):
        earlier = {}
        for o, r in manual[:i]:
            if pos[o] > pos[out]:
                for k, v in r.items():
                    earlier.setdefault(k, []).extend(v)
        if earlier:
            rule = {'type': 'logical', 'mode': 'and', 'rules': [rule, dict(earlier, invert=True)]}
        sets[out].append(rule)

    def rules_path(t):
        return os.path.join(RULES_DIR, f'{t}.json')

    # Ручные маршруты — сразу за служебной головой списка (sniff, mon-*,
    # блокировки loopback и 853, выход бота), но выше всего остального (YouTube, .ru,
    # дорожки). Выше головы маршрут панели на домен или подсеть перехватывал бы
    # скрейп экзитов и бота, а 0.0.0.0/0 на экзит открывал бы его loopback.
    route = cfg['route']
    route['rule_set'] = route.get('rule_set', []) + [
        {'type': 'local', 'tag': f'manual-{t}', 'format': 'source', 'path': rules_path(t)}
        for t in targets]
    rules = route['rules']
    head = 0
    while head < len(rules) and ('inbound' in rules[head] or rules[head].get('action') == 'reject'):
        head += 1
    route['rules'] = rules[:head] + [{'rule_set': [f'manual-{t}'], 'outbound': t} for t in targets] + rules[head:]

    # Предпочитаемые экзиты клиентов (панель пишет client-prefs.json:
    # {"<узел>": ["<IP устройства>", ...]}). IP узла — в набор client-pref-<узел>
    # с выходом pin-hy2-<узел>-direct: пока узел выключен, мёртв или в штрафной,
    # вотчдог держит pin на foreign-best. Наборы заведены на каждый узел, даже
    # пустые, и перечитываются на ходу: смена предпочтения не трогает config.json
    # и не рвёт соединения. Правила — перед дорожками, то есть после ручных
    # маршрутов, YouTube и РФ: меняется только то, что ушло бы в дорожку.
    nodes = sorted(t[len('pin-hy2-'):-len('-direct')] for t in known
                   if t.startswith('pin-hy2-') and t.endswith('-direct'))
    pref_sets = {n: set() for n in nodes}
    for node, ips in prefs.items() if isinstance(prefs, dict) else []:
        if node not in pref_sets:
            print(f'пропускаю предпочтение {node!r}: выхода hy2-{node}-direct нет в конфиге', file=sys.stderr)
            continue
        for ip in ips if isinstance(ips, list) else []:
            try:
                pref_sets[node].add(f'{ipaddress.ip_address(str(ip))}/32')
            except ValueError:
                print(f'пропускаю IP {ip!r} предпочтения {node!r}', file=sys.stderr)

    def pref_path(n):
        return os.path.join(PREFS_DIR, f'{n}.json')

    route['rule_set'] += [
        {'type': 'local', 'tag': f'client-pref-{n}', 'format': 'source', 'path': pref_path(n)}
        for n in nodes]
    rules = route['rules']
    at = next((i for i, r in enumerate(rules) if str(r.get('outbound', '')).startswith('lane-')), len(rules))
    rules[at:at] = [{'rule_set': [f'client-pref-{n}'], 'outbound': f'pin-hy2-{n}-direct'} for n in nodes]

    new_body = json.dumps(cfg, indent=2)

    def read(path):
        try:
            with open(path) as f: return f.read()
        except FileNotFoundError:
            return None

    def put(path, body, check=None):
        """Атомарно (tmp + rename в том же каталоге) заменить файл, если изменился."""
        if read(path) == body:
            return False
        with tempfile.NamedTemporaryFile('w', dir=os.path.dirname(path), delete=False, suffix='.tmp') as tmp:
            tmp.write(body); tmp_path = tmp.name
        try:
            if check:
                subprocess.run(check + [tmp_path], check=True, capture_output=True)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except subprocess.CalledProcessError as e:
            os.unlink(tmp_path)
            print(f'{path}: проверка не прошла: {e.stderr.decode(errors="replace").strip()}', file=sys.stderr)
            sys.exit(1)
        return True

    # Сначала наборы: и живой sing-box, и проверка нового config.json читают их
    # с диска. Панель при сохранении пишет manual-routes.json несколько раз
    # подряд, неизменённые файлы не трогаем.
    os.makedirs(RULES_DIR, mode=0o700, exist_ok=True)
    changed = [t for t in targets
               if put(rules_path(t), json.dumps({'version': 2, 'rules': sets[t]}, indent=2),
                      [SB_BIN, 'rule-set', 'compile', '-o', os.devnull])]
    for name in os.listdir(RULES_DIR):
        if name.endswith('.json') and name[:-5] not in pos:
            os.unlink(os.path.join(RULES_DIR, name))
    os.makedirs(PREFS_DIR, mode=0o700, exist_ok=True)
    for n in nodes:
        body = {'version': 2, 'rules': [{'source_ip_cidr': sorted(pref_sets[n])}] if pref_sets[n] else []}
        if put(pref_path(n), json.dumps(body, indent=2), [SB_BIN, 'rule-set', 'compile', '-o', os.devnull]):
            changed.append(f'client-pref-{n}')
    for name in os.listdir(PREFS_DIR):
        if name.endswith('.json') and name[:-5] not in pref_sets:
            os.unlink(os.path.join(PREFS_DIR, name))

    # Снимок того, что получил sing-box: итоговые route.rules и содержимое всех
    # наборов. По нему панель отвечает, через какой выход пойдёт адрес, тем же
    # порядком правил, без своей копии. Пишется только вместе с принятым конфигом.
    def set_rules(item):
        path = item.get('path', '')
        if item.get('format') == 'binary':
            out = subprocess.run([SB_BIN, 'rule-set', 'decompile', '-o', '/dev/stdout', path],
                                 check=True, capture_output=True, text=True).stdout
            return json.loads(out).get('rules', [])
        with open(path) as f:
            return json.load(f).get('rules', [])

    def write_plan():
        try:
            sets_body = {item['tag']: set_rules(item) for item in route.get('rule_set', [])}
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
            print(f'{ROUTE_PLAN}: набор не читается, снимок маршрутов прежний: {e}', file=sys.stderr)
            return
        plan = {'rules': route['rules'], 'final': route.get('final', ''), 'rule_set': sets_body}
        put(ROUTE_PLAN, json.dumps(plan, ensure_ascii=False, separators=(',', ':')))

    summary = (f'{len(manual)} manual route(s), '
               f'{sum(map(len, pref_sets.values()))} IP с предпочтением, наборов изменено: {len(changed)}')
    if read(SB_CONFIG) == new_body:
        tail = 'config.json уже собран' if NO_RESTART else 'sing-box перечитает их сам, без перезапуска'
        write_plan()
        print(f'{summary}, {tail}')
        sys.exit(0)

    # config.json меняется только при смене выходов в базе (передеплой) — тогда
    # без перезапуска не обойтись. Через vpn2-apply (стадия 29-control): кандидат,
    # проверка, перезапуск, проверка службы; не поднялась — возвращается последний
    # рабочий. Первая установка идёт до 29-control, восстанавливать там нечего.
    APPLY = '/usr/local/sbin/vpn2-apply'
    if not NO_RESTART and os.access(APPLY, os.X_OK):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as candidate:
            candidate.write(new_body)
            candidate.flush()
            applied = subprocess.run([APPLY, 'install', 'sing-box', candidate.name])
        if applied.returncode:
            sys.exit(applied.returncode)
        write_plan()
        print(f'{summary}, config.json изменился, sing-box restarted')
        sys.exit(0)
    put(SB_CONFIG, new_body, [SB_BIN, 'check', '-c'])
    write_plan()
    if NO_RESTART:
        print(f'{summary}, config.json собран')
        sys.exit(0)
    subprocess.run(['systemctl', 'restart', 'sing-box'], check=True)
    print(f'{summary}, config.json изменился, sing-box restarted')
    return 0


if __name__ == '__main__':
    sys.exit(main())
