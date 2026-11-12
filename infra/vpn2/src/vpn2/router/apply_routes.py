"""Ручные маршруты и предпочтения панели -> наборы sing-box, config.json и route-plan.json.

`vpn2 router apply-routes [--no-restart]`; --no-restart только собирает config.json
(перезапуск делает установка, один раз на итоговом конфиге). Маршруты лежат в local
rule-set'ах по набору на выход: изменённый набор sing-box перечитывает сам, не рвя
соединений, а config.json меняется только при смене выходов в базе.
"""
import ipaddress
import json
import os
import subprocess
import sys
import tempfile

MANUAL_ROUTES = '/etc/anysda/manual-routes.json'
CLIENT_PREFS = '/etc/anysda/client-prefs.json'
SB_CONFIG = '/etc/sing-box/config.json'
SB_CONFIG_BASE = '/etc/sing-box/config-base.json'
RULES_DIR = '/etc/sing-box/manual-routes'
PREFS_DIR = '/etc/sing-box/client-prefs'
ROUTE_PLAN = '/etc/anysda/route-plan.json'
SB_BIN = '/usr/local/bin/sing-box'
APPLY = '/usr/local/sbin/vpn2-apply'


class Refusal(Exception):
    """Действующие правила остаются; текст уходит в журнал."""


def load(path, kind):
    """Пропавший файл - пустой список; битый или недописанный не значит «удалить всё»."""
    try:
        with open(path) as f:
            value = json.load(f)
        if not isinstance(value, kind):
            raise ValueError(f'ожидался {kind.__name__}')
    except FileNotFoundError:
        return kind()
    except (ValueError, OSError) as e:
        raise Refusal(f'{path}: не читается список маршрутов: {e}; сохраняю действующие правила')
    return value


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def put(path, body, check=None):
    """Атомарная замена файла, если он изменился; check - проверка кандидата."""
    if read(path) == body:
        return False
    with tempfile.NamedTemporaryFile('w', dir=os.path.dirname(path), delete=False, suffix='.tmp') as tmp:
        tmp.write(body)
    try:
        if check:
            subprocess.run([*check, tmp.name], check=True, capture_output=True)
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, path)
    except subprocess.CalledProcessError as e:
        os.unlink(tmp.name)
        raise Refusal(f'{path}: проверка не прошла: {e.stderr.decode(errors="replace").strip()}')
    return True


def manual_rules(rows, known):
    """Строки панели -> [(выход, правило)]; строка с неизвестным выходом пропускается одна."""
    manual = []
    for row in rows:
        try:
            out, kind, value = row['outbound'], row['type'], str(row['value'])
            if out not in known:
                raise ValueError(f'outbound {out!r} нет в конфиге')
            # pin-<выход> сторож переводит на foreign-best, пока выход мёртв.
            if f'pin-{out}' in known:
                out = f'pin-{out}'
            if kind == 'domain':
                rule = {'domain_suffix': [value.removeprefix('*.')]}
            elif kind == 'ip_cidr':
                ipaddress.ip_network(value, strict=False)
                rule = {'ip_cidr': [value]}
            else:
                raise ValueError(f'тип {kind!r} неизвестен')
        except (KeyError, TypeError, ValueError) as e:
            print(f'пропускаю маршрут {row!r}: {e}', file=sys.stderr)
            continue
        manual.append((out, rule))
    return manual


def route_sets(cfg, known, manual):
    """Набор на каждый выход панели, даже пустой: правка маршрута меняет только файлы.

    Побеждает первая строка панели, а наборы стоят в порядке выходов: из строки
    вычитаются более ранние строки наборов, стоящих ниже её.
    """
    targets = []
    for tag in (o.get('tag', '') for o in cfg.get('outbounds', [])):
        if tag == 'direct-ru' or (tag.startswith('hy2-') and not tag.endswith('-mgmt')):
            targets.append(f'pin-{tag}' if f'pin-{tag}' in known else tag)
    targets += [out for out, _ in manual if out not in targets]
    targets = list(dict.fromkeys(targets))
    pos = {t: i for i, t in enumerate(targets)}
    sets = {t: [] for t in targets}
    for i, (out, rule) in enumerate(manual):
        earlier = {}
        for other, prior in manual[:i]:
            if pos[other] > pos[out]:
                for key, values in prior.items():
                    earlier.setdefault(key, []).extend(values)
        sets[out].append({'type': 'logical', 'mode': 'and', 'rules': [rule, dict(earlier, invert=True)]}
                         if earlier else rule)
    return sets


def pref_sets(known, prefs):
    """Предпочитаемые экзиты {узел: [IP]} -> source-наборы client-pref-<узел>."""
    nodes = sorted(t[len('pin-hy2-'):-len('-direct')] for t in known
                   if t.startswith('pin-hy2-') and t.endswith('-direct'))
    sets = {n: set() for n in nodes}
    for node, ips in prefs.items():
        if node not in sets:
            print(f'пропускаю предпочтение {node!r}: выхода hy2-{node}-direct нет в конфиге', file=sys.stderr)
            continue
        for ip in ips if isinstance(ips, list) else []:
            try:
                sets[node].add(f'{ipaddress.ip_address(str(ip))}/32')
            except ValueError:
                print(f'пропускаю IP {ip!r} предпочтения {node!r}', file=sys.stderr)
    return sets


def write_sets(directory, sets, body):
    """Наборы на диск (сначала они: их читают и живой sing-box, и проверка config.json)."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    changed = [name for name, rules in sets.items()
               if put(os.path.join(directory, f'{name}.json'), json.dumps(body(rules), indent=2),
                      [SB_BIN, 'rule-set', 'compile', '-o', os.devnull])]
    for name in os.listdir(directory):
        if name.endswith('.json') and name[:-5] not in sets:
            os.unlink(os.path.join(directory, name))
    return changed


def write_plan(route):
    """Снимок итоговых route.rules и всех наборов: по нему панель отвечает, куда пойдёт адрес."""
    def rules(item):
        if item.get('format') == 'binary':
            out = subprocess.run([SB_BIN, 'rule-set', 'decompile', '-o', '/dev/stdout', item.get('path', '')],
                                 check=True, capture_output=True, text=True).stdout
            return json.loads(out).get('rules', [])
        with open(item.get('path', '')) as f:
            return json.load(f).get('rules', [])
    try:
        sets = {item['tag']: rules(item) for item in route.get('rule_set', [])}
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f'{ROUTE_PLAN}: набор не читается, снимок маршрутов прежний: {e}', file=sys.stderr)
        return
    plan = {'rules': route['rules'], 'final': route.get('final', ''), 'rule_set': sets}
    put(ROUTE_PLAN, json.dumps(plan, ensure_ascii=False, separators=(',', ':')))


def apply(no_restart):
    if not os.path.exists(SB_CONFIG_BASE):
        raise Refusal('config-base.json missing')
    rows, prefs = load(MANUAL_ROUTES, list), load(CLIENT_PREFS, dict)
    with open(SB_CONFIG_BASE) as f:
        cfg = json.load(f)
    known = {o.get('tag') for o in cfg.get('outbounds', []) + cfg.get('endpoints', [])}
    manual = manual_rules(rows, known)
    sets, prefer = route_sets(cfg, known, manual), pref_sets(known, prefs)
    route = cfg['route']
    # Ручные маршруты - сразу за служебной головой (sniff, mon-*, блокировки, выход
    # бота), предпочтения - перед дорожками: меняется только то, что ушло бы в дорожку.
    route['rule_set'] = route.get('rule_set', []) + [
        {'type': 'local', 'tag': f'manual-{t}', 'format': 'source', 'path': os.path.join(RULES_DIR, f'{t}.json')}
        for t in sets] + [
        {'type': 'local', 'tag': f'client-pref-{n}', 'format': 'source', 'path': os.path.join(PREFS_DIR, f'{n}.json')}
        for n in prefer]
    rules = route['rules']
    head = next((i for i, r in enumerate(rules) if 'inbound' not in r and r.get('action') != 'reject'), len(rules))
    rules[head:head] = [{'rule_set': [f'manual-{t}'], 'outbound': t} for t in sets]
    lanes = next((i for i, r in enumerate(rules) if str(r.get('outbound', '')).startswith('lane-')), len(rules))
    rules[lanes:lanes] = [{'rule_set': [f'client-pref-{n}'], 'outbound': f'pin-hy2-{n}-direct'} for n in prefer]
    changed = write_sets(RULES_DIR, sets, lambda r: {'version': 2, 'rules': r})
    changed += write_sets(PREFS_DIR, prefer,
                          lambda ips: {'version': 2, 'rules': [{'source_ip_cidr': sorted(ips)}] if ips else []})
    summary = (f'{len(manual)} manual route(s), {sum(map(len, prefer.values()))} IP с предпочтением, '
               f'наборов изменено: {len(changed)}')
    body = json.dumps(cfg, indent=2)
    if read(SB_CONFIG) == body:
        write_plan(route)
        return f"{summary}, {'config.json уже собран' if no_restart else 'sing-box перечитает их сам, без перезапуска'}"
    # vpn2-apply: кандидат, проверка, перезапуск; не поднялся - возвращается последний рабочий.
    if not no_restart and os.access(APPLY, os.X_OK):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as candidate:
            candidate.write(body)
            candidate.flush()
            if subprocess.run([APPLY, 'install', 'sing-box', candidate.name]).returncode:
                raise Refusal('')
    else:
        put(SB_CONFIG, body, [SB_BIN, 'check', '-c'])
        if not no_restart:
            subprocess.run(['systemctl', 'restart', 'sing-box'], check=True)
    write_plan(route)
    return f"{summary}, {'config.json собран' if no_restart else 'config.json изменился, sing-box restarted'}"


def main(argv=None):
    try:
        print(apply('--no-restart' in (sys.argv[1:] if argv is None else argv)))
    except Refusal as e:
        if str(e):
            print(e, file=sys.stderr)
        return 1
    return 0
