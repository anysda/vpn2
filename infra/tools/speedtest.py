#!/usr/bin/env python3
"""speedtest.py — эталонный замер скорости через туннели на стенде.

Запускается с машины оператора, на узлы и клиентов ходит по ssh. Ничего не
ставит и не меняет, кроме предпочитаемого экзита у тестовых клиентов панели.

Что нужно заранее:
  - хаб (entry) стенда, на нём панель и `ssh <тег>` до экзитов (стадия 20);
  - клиенты с поднятым туннелем (WireGuard, OpenVPN, IKEv2), на каждом
    iperf3, curl, ping, python3; каждый клиент — отдельный клиент панели;
  - iperf3-сервер вне РФ, слушает порты --iperf-port и следующие за ним.

Пример:
  infra/tools/speedtest.py --hub root@HUB --iperf IPERF_HOST \\
      --client wg=root@CLIENT1/speed-wg \\
      --client ovpn=root@CLIENT2/speed-ovpn \\
      --client ikev2=root@CLIENT3/speed-ikev2 \\
      --out before.json > before.md

  infra/tools/speedtest.py --compare before.json after.json

Клиент задаётся как МЕТКА=SSH/ИМЯ_КЛИЕНТА_В_ПАНЕЛИ. Экзит переключается через
API панели (preferredExit) на самом хабе; пароль админа не покидает хаб.
"""

import argparse
import concurrent.futures as cf
import json
import re
import shlex
import statistics
import subprocess
import sys
import time

SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
       '-o', 'ServerAliveInterval=15']

FOREIGN_SITES = ['https://www.google.com', 'https://www.cloudflare.com',
                 'https://github.com', 'https://www.wikipedia.org']
RU_SITES = ['https://ya.ru', 'https://vk.com']

DNS_DOMAINS = (
    'google.com youtube.com facebook.com wikipedia.org amazon.com yahoo.com '
    'reddit.com instagram.com linkedin.com netflix.com twitter.com bing.com '
    'microsoft.com apple.com github.com stackoverflow.com cloudflare.com '
    'mozilla.org debian.org ubuntu.com python.org nodejs.org npmjs.com '
    'docker.com gitlab.com spotify.com twitch.tv tiktok.com whatsapp.com '
    'telegram.org zoom.us dropbox.com adobe.com paypal.com ebay.com imdb.com '
    'nytimes.com bbc.co.uk cnn.com theguardian.com yandex.ru ya.ru vk.com '
    'mail.ru ok.ru avito.ru ozon.ru wildberries.ru sberbank.ru gosuslugi.ru'
).split()

# Клиентская часть DNS-пачки: n запросов A равномерно за window секунд,
# ответ ждём timeout секунд после последней отправки. Без повторов: считаем,
# сколько ответов пришло с первого раза и за какое время.
DNS_PROBE = r'''
import json, random, select, socket, struct, sys, time
server, n, window, timeout, start_at = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
names = sys.argv[6].split(',')
def query(qid, name):
    q = struct.pack('>HHHHHH', qid, 0x0100, 1, 0, 0, 0)
    for part in name.split('.'):
        q += bytes([len(part)]) + part.encode()
    return q + b'\x00' + struct.pack('>HH', 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setblocking(False)
ids = random.sample(range(65536), n)
sent, lat = {}, {}
while time.time() < start_at:
    time.sleep(0.005)
t0 = time.time()
i = 0
while True:
    now = time.time()
    while i < n and now - t0 >= i * window / n:
        s.sendto(query(ids[i], names[i % len(names)]), (server, 53))
        sent[ids[i]] = time.time()
        i += 1
    if i == n and (len(lat) == n or now - t0 > window + timeout):
        break
    r, _, _ = select.select([s], [], [], 0.002)
    if r:
        data = s.recv(4096)
        qid = struct.unpack('>H', data[:2])[0]
        if qid in sent and qid not in lat:
            lat[qid] = (time.time() - sent[qid]) * 1000
print(json.dumps({'sent': n, 'lat_ms': sorted(lat.values())}))
'''

# Снимок счётчиков узла: UDP/TCP из /proc/net/snmp, qdisc основного
# интерфейса (по маршруту по умолчанию), ЦПУ из /proc/stat и буферы приёма
# UDP-сокетов sing-box (hy2).
NODE_SNAPSHOT = r'''
iface=$(ip -4 route show default | awk '{print $5; exit}')
echo "IFACE $iface"
grep -E '^(Udp|Tcp):' /proc/net/snmp
tc -s qdisc show dev "$iface"
head -1 /proc/stat
ss -uanmp 2>/dev/null | grep -A1 sing-box | grep -o 'rb[0-9]*' | sed 's/^/SKRB /'
'''


def sh(target, cmd, *, via=None, stdin=None, timeout=600):
    """Команда на узле по ssh; via — хаб, через который ходят на экзит."""
    if via:
        argv = SSH + [via, f'ssh -o BatchMode=yes {shlex.quote(target)} {shlex.quote(cmd)}']
    else:
        argv = SSH + [target, cmd]
    res = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f'{target}: {cmd[:60]}…: rc={res.returncode}: {res.stderr.strip()[-300:]}')
    return res.stdout


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', file=sys.stderr, flush=True)


# --- панель ------------------------------------------------------------------

PANEL_CALL = r'''
set -e
# Сессия живёт между вызовами: панель считает в лимит попыток и удачные входы,
# и вход на каждое переключение экзита за прогон упирается в 429.
umask 077
jar=/root/.speedtest-panel.jar
if ! curl -fsS -b "$jar" -c "$jar" http://127.0.0.1:51821/api/clients > /tmp/.speedtest-clients.json 2>/dev/null; then
  pw=$(cat /etc/anysda/admin-password.txt)
  user=$(awk '/^admin:/{a=1;next} a&&/^[^ ]/{a=0} a&&/user:/{print $2; exit}' /opt/anysda-vpn2/config.yaml)
  python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1] or "anysda", "password": sys.argv[2]}))' "$user" "$pw" \
    | curl -fsS -c "$jar" -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:51821' \
      --data @- http://127.0.0.1:51821/api/auth/sign-in/username >/dev/null
  curl -fsS -b "$jar" http://127.0.0.1:51821/api/clients > /tmp/.speedtest-clients.json
fi
for pair in $PATCHES; do
  id=${pair%%=*}; exit_tag=${pair#*=}
  curl -fsS -b "$jar" -X PATCH -H 'Content-Type: application/json' \
    --data "{\"preferredExit\": \"$exit_tag\"}" "http://127.0.0.1:51821/api/clients/$id" >/dev/null
done
cat /tmp/.speedtest-clients.json; rm -f /tmp/.speedtest-clients.json
'''


def panel_set_exit(hub, client_names, exit_tag):
    """Ставит preferredExit всем тестовым клиентам; возвращает id по имени."""
    clients = json.loads(sh(hub, 'PATCHES= bash -s', stdin=PANEL_CALL))
    ids = {c['name']: c['id'] for c in clients}
    missing = [n for n in client_names if n not in ids]
    if missing:
        raise SystemExit(f'в панели нет клиентов: {", ".join(missing)}')
    patches = ' '.join(f'{ids[n]}={exit_tag}' for n in client_names)
    sh(hub, f'PATCHES={shlex.quote(patches)} bash -s', stdin=PANEL_CALL)


# --- клиент ------------------------------------------------------------------

def iperf(client, host, port, *, secs, streams=1, reverse=False, udp_rate=None):
    cmd = f'iperf3 -J -c {host} -p {port} -t {secs} -O 2 -P {streams}'
    if reverse:
        cmd += ' -R'
    if udp_rate:
        cmd += f' -u -b {udp_rate}'
    for attempt in range(3):  # сервер iperf3 занят/не отвечает — повтор
        try:
            data = json.loads(sh(client, cmd, timeout=secs + 60))
        except (RuntimeError, json.JSONDecodeError) as err:
            log(f'  iperf3 {client} повтор {attempt + 1}: {err}')
            time.sleep(3)
            continue
        end = data['end']
        if udp_rate:
            s = end.get('sum_received') or end['sum']
            return {'mbps': s['bits_per_second'] / 1e6,
                    'loss': s.get('lost_percent', 0.0), 'jitter': s.get('jitter_ms', 0.0)}
        return {'mbps': end['sum_received']['bits_per_second'] / 1e6,
                'retr': end.get('sum_sent', {}).get('retransmits', 0)}
    return None


def ping(client, host, count=20):
    out = sh(client, f'ping -n -q -i 0.2 -c {count} -W 2 {host} || true', timeout=count + 30)
    loss = re.search(r'([\d.]+)% packet loss', out)
    rtt = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
    return {'loss': float(loss.group(1)) if loss else 100.0,
            'avg': float(rtt.group(2)) if rtt else None,
            'mdev': float(rtt.group(4)) if rtt else None}


def ttfb(client, url, tries=3):
    vals = []
    for _ in range(tries):
        out = sh(client, f"curl -o /dev/null -sS --max-time 15 -w '%{{time_starttransfer}}' {shlex.quote(url)} || echo X")
        if 'X' not in out:
            vals.append(float(out) * 1000)
    return statistics.median(vals) if vals else None


def egress_ip(client, url):
    return sh(client, f'curl -s --max-time 8 {shlex.quote(url)} || true').strip()


def dns_burst(clients, server, n, window, timeout):
    names = ','.join(f'{p}{d}' for d in DNS_DOMAINS for p in ('', 'www.'))
    start_at = time.time() + 3
    args = f'{server} {n} {window} {timeout} {start_at} {names}'
    with cf.ThreadPoolExecutor(len(clients)) as ex:
        futs = {lbl: ex.submit(sh, ssh, f'python3 - {args}', stdin=DNS_PROBE)
                for lbl, ssh in clients}
        res = {lbl: json.loads(f.result()) for lbl, f in futs.items()}
    return res


# --- узлы --------------------------------------------------------------------

def snapshot(node, via):
    out = sh(node, 'bash -s', via=via, stdin=NODE_SNAPSHOT)
    snap = {'drops': 0, 'plimit': 0, 'skrb': []}
    hdr = {}
    for line in out.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] in ('Udp:', 'Tcp:'):
            if f[0] not in hdr:
                hdr[f[0]] = f[1:]
            else:
                for k, v in zip(hdr[f[0]], f[1:]):
                    snap[f[0][:-1] + k] = int(v)
        elif f[0] == 'Sent' and 'dropped' in f:
            snap['drops'] += int(f[f.index('dropped') + 1].rstrip(','))
        elif 'flows_plimit' in f:
            snap['plimit'] += int(f[f.index('flows_plimit') + 1])
        elif f[0] == 'cpu':
            snap['cpu'] = [int(x) for x in f[1:9]]
        elif f[0] == 'SKRB':
            snap['skrb'].append(int(f[1][2:]))
        elif f[0] == 'IFACE':
            snap['iface'] = f[1] if len(f) > 1 else '?'
        elif f[0] == 'qdisc' and f[1] != 'fq_codel' and 'root' in f:
            snap['qdisc'] = ' '.join(f[1:2] + (f[f.index('flow_limit'):f.index('flow_limit') + 2] if 'flow_limit' in f else []))
    return snap


def snap_delta(a, b):
    return {
        'UdpRcvbufErrors': b.get('UdpRcvbufErrors', 0) - a.get('UdpRcvbufErrors', 0),
        'UdpInErrors': b.get('UdpInErrors', 0) - a.get('UdpInErrors', 0),
        'TcpRetransSegs': b.get('TcpRetransSegs', 0) - a.get('TcpRetransSegs', 0),
        'drops': b['drops'] - a['drops'],
        'plimit': b['plimit'] - a['plimit'],
        'cpu_raw': [y - x for x, y in zip(a['cpu'], b['cpu'])],
        'skrb': min(b['skrb']) if b['skrb'] else None,
        'qdisc': f"{b.get('iface', '?')}: {b.get('qdisc', '?')}",
    }


def merge_delta(a, b):
    """Сумма приростов двух кругов одной ячейки."""
    out = dict(b)
    for k in ('UdpRcvbufErrors', 'UdpInErrors', 'TcpRetransSegs', 'drops', 'plimit'):
        out[k] = a[k] + b[k]
    out['cpu_raw'] = [x + y for x, y in zip(a['cpu_raw'], b['cpu_raw'])]
    rbs = [v for v in (a['skrb'], b['skrb']) if v is not None]
    out['skrb'] = min(rbs) if rbs else None
    return out


def cpu_pct(raw):
    # поля /proc/stat: user nice system idle iowait irq softirq steal
    tot = sum(raw) or 1
    pct = [round(100 * v / tot) for v in raw]
    return f'us{pct[0] + pct[1]} sy{pct[2]} si{pct[6]} st{pct[7]} id{pct[3]}'


def switch_exit(args, hub, clients, ex, ip):
    """Переключает клиентов на экзит и ждёт, пока их выход сменится."""
    panel_set_exit(hub, [c[2] for c in clients], ex)
    if not args.ip_echo:
        return
    deadline = time.time() + 60
    pending = {c[1] for c in clients}
    while pending and time.time() < deadline:
        pending = {c for c in pending if egress_ip(c, args.ip_echo) != ip}
        if pending:
            time.sleep(3)
    if pending:
        raise SystemExit(f'клиенты {pending} не вышли через {ex} ({ip}) за 60 с')


# --- прогон ------------------------------------------------------------------

TESTS = (('tcp1_down', {'reverse': True}), ('tcp1_up', {}),
         ('tcp8_down', {'reverse': True, 'streams': 8}), ('tcp8_up', {'streams': 8}),
         ('udp_down', {'reverse': True}))


def run(args):
    clients = []
    for spec in args.client:
        lbl, rest = spec.split('=', 1)
        ssh, panel_name = rest.rsplit('/', 1)
        clients.append((lbl, ssh, panel_name))
    hub = args.hub
    exits = args.exits.split(',') if args.exits else sh(
        hub, "python3 -c \"import yaml;print(' '.join(e['tag'] for e in "
             "yaml.safe_load(open('/opt/anysda-vpn2/config.yaml'))['exits']))\"").split()
    exit_ip = {t: sh(t, "ip -4 route get 1.1.1.1 | grep -o 'src [0-9.]*' | cut -d' ' -f2",
                     via=hub).strip() for t in exits}
    res = {'meta': {'time': time.strftime('%Y-%m-%d %H:%M'), 'secs': args.time,
                    'repeat': args.repeat,
                    'udp_rate': args.udp_rate, 'exits': exits,
                    'clients': [c[0] for c in clients],
                    'hub_rev': sh(hub, 'git -C /opt/anysda-vpn2 log -1 --format=%h || true').strip()},
           'tput': {}, 'lat': {}, 'nodes': {}, 'dns': {}}

    # Путь до iperf-сервера проседает на минуты целиком (весь блок экзита
    # в одном прогоне вдвое медленнее, чем в соседнем). Поэтому круги идут
    # снаружи: каждый круг проходит все экзиты и всех клиентов, повторы одной
    # ячейки разнесены по всему прогону, и медиана отсекает такую просадку.
    samples, nodes = {}, {}
    for rnd in range(args.repeat):
        for ex in exits:
            log(f'круг {rnd + 1}/{args.repeat}, экзит {ex}: переключаю клиентов')
            switch_exit(args, hub, clients, ex, exit_ip[ex])
            for port_off, (lbl, ssh, _) in enumerate(clients):
                key = f'{lbl}/{ex}'
                port = args.iperf_port + port_off
                if rnd == 0:
                    log(f'{key}: задержка')
                    res['lat'][key] = {'ping': ping(ssh, args.dns),
                                       'foreign': [ttfb(ssh, u) for u in FOREIGN_SITES],
                                       'ru': [ttfb(ssh, u) for u in RU_SITES]}
                before = {'hub': snapshot(hub, None), 'exit': snapshot(ex, hub)}
                for name, kw in TESTS:
                    log(f'{key}: {name}')
                    if name == 'udp_down':
                        kw = dict(kw, udp_rate=args.udp_rate)
                    r = iperf(ssh, args.iperf, port, secs=args.time, **kw)
                    if r:
                        samples.setdefault(key, {}).setdefault(name, []).append(r)
                after = {'hub': snapshot(hub, None), 'exit': snapshot(ex, hub)}
                for k in before:
                    d = snap_delta(before[k], after[k])
                    prev = nodes.setdefault(key, {}).get(k)
                    nodes[key][k] = merge_delta(prev, d) if prev else d
    for key in (f'{c[0]}/{ex}' for ex in exits for c in clients):
        row = {}
        for name, _ in TESTS:
            got = sorted(samples.get(key, {}).get(name, []), key=lambda r: r['mbps'])
            row[name] = dict(got[len(got) // 2], runs=[r['mbps'] for r in got]) if got else None
        res['tput'][key] = row
        res['nodes'][key] = {k: dict(d, cpu=cpu_pct(d['cpu_raw'])) for k, d in nodes.get(key, {}).items()}

    log('DNS: пачка с одного клиента')
    first = clients[0]
    res['dns']['one'] = dns_burst([(first[0], first[1])], args.dns, args.dns_n, 1.0, 2.0)
    time.sleep(3)
    log('DNS: пачка с трёх клиентов разом')
    res['dns']['all'] = dns_burst([(c[0], c[1]) for c in clients], args.dns, args.dns_n, 1.0, 2.0)
    return res


# --- вывод -------------------------------------------------------------------

def f0(v):
    return '—' if v is None else f'{v:.0f}'


def render(res):
    m = res['meta']
    out = [f"Замер {m['time']}, хаб на `{m['hub_rev']}`, iperf3 -t {m['secs']} -O 2 ×{m.get('repeat', 1)} (медиана), UDP {m['udp_rate']}", '']
    out += ['**Пропускная, Мбит/с** (↓ к клиенту, ↑ от клиента; в скобках ретрансмиты/потери)', '',
            '| клиент/экзит | TCP×1 ↓ | TCP×1 ↑ | TCP×8 ↓ | TCP×8 ↑ | UDP ↓ |',
            '|---|---|---|---|---|---|']
    for key, r in res['tput'].items():
        cells = []
        for k in ('tcp1_down', 'tcp1_up', 'tcp8_down', 'tcp8_up'):
            v = r.get(k)
            cells.append('—' if not v else f"{v['mbps']:.0f} ({v['retr']})")
        u = r.get('udp_down')
        cells.append('—' if not u else f"{u['mbps']:.0f} ({u['loss']:.1f}%)")
        out.append(f'| {key} | ' + ' | '.join(cells) + ' |')
    out += ['', '**Задержка**: ping до хаба по туннелю (ICMP наружу sing-box не ведёт), TTFB HTTPS (медиана из 3), мс', '',
            '| клиент/экзит | ping avg | mdev | потери | ' + ' | '.join(u.split('//')[1] for u in FOREIGN_SITES + RU_SITES) + ' |',
            '|---|---|---|---|' + '---|' * (len(FOREIGN_SITES) + len(RU_SITES))]
    for key, lat in res['lat'].items():
        p = lat['ping']
        out.append(f"| {key} | {f0(p['avg'])} | {f0(p['mdev'])} | {p['loss']:.0f}% | "
                   + ' | '.join(f0(v) for v in lat['foreign'] + lat['ru']) + ' |')
    out += ['', '**DNS**: пачка запросов A за 1 с, ответ ждём 2 с, без повторов', '',
            '| пачка | клиент | ответов | p50, мс | p95, мс | max, мс |', '|---|---|---|---|---|---|']
    for kind, title in (('one', 'один клиент'), ('all', 'три клиента разом')):
        for lbl, d in res['dns'].get(kind, {}).items():
            lat = d['lat_ms']
            q = statistics.quantiles(lat, n=20) if len(lat) > 1 else lat * 19
            out.append(f"| {title} | {lbl} | {len(lat)}/{d['sent']} | "
                       f"{f0(statistics.median(lat) if lat else None)} | {f0(q[18] if lat else None)} | "
                       f"{f0(max(lat) if lat else None)} |")
    out += ['', '**Узлы**: приросты за все iperf3 одной строки (сумма по кругам), ЦПУ в % за них же', '',
            '| клиент/экзит | узел | qdisc | UdpRcvbufErrors | UdpInErrors | TcpRetrans | qdisc drops | flows_plimit | ЦПУ | мин. rb sing-box |',
            '|---|---|---|---|---|---|---|---|---|---|']
    for key, nodes in res['nodes'].items():
        for role, d in nodes.items():
            out.append(f"| {key} | {role} | {d['qdisc']} | {d['UdpRcvbufErrors']} | {d['UdpInErrors']} | "
                       f"{d['TcpRetransSegs']} | {d['drops']} | {d['plimit']} | {d['cpu']} | "
                       f"{d['skrb'] if d['skrb'] is not None else '—'} |")
    return '\n'.join(out) + '\n'


def compare(a_path, b_path):
    """Разброс пропускной между двумя прогонами: |a-b| / среднее."""
    a, b = (json.load(open(p)) for p in (a_path, b_path))
    out = [f"Сравнение `{a_path}` ({a['meta']['time']}) и `{b_path}` ({b['meta']['time']}), Мбит/с", '',
           '| клиент/экзит | тест | A | B | Δ, % |', '|---|---|---|---|---|']
    spreads = []
    for key, ra in a['tput'].items():
        rb = b['tput'].get(key, {})
        for test, va in ra.items():
            vb = rb.get(test)
            if not va or not vb:
                continue
            mean = (va['mbps'] + vb['mbps']) / 2 or 1
            d = 100 * (vb['mbps'] - va['mbps']) / mean
            spreads.append(abs(d))
            out.append(f"| {key} | {test} | {va['mbps']:.0f} | {vb['mbps']:.0f} | {d:+.1f} |")
    if spreads:
        out += ['', f'Медиана |Δ| {statistics.median(spreads):.1f}%, максимум {max(spreads):.1f}%, '
                    f'строк с |Δ| > 10%: {sum(s > 10 for s in spreads)} из {len(spreads)}']
    return '\n'.join(out) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--hub', help='ssh-цель хаба (entry), например root@1.2.3.4')
    p.add_argument('--client', action='append', default=[],
                   help='МЕТКА=SSH/ИМЯ_В_ПАНЕЛИ, повторяемый')
    p.add_argument('--iperf', help='адрес iperf3-сервера вне РФ')
    p.add_argument('--iperf-port', type=int, default=5201,
                   help='первый порт; клиенту N достаётся порт +N')
    p.add_argument('--exits', help='теги экзитов через запятую (по умолчанию все из config.yaml хаба)')
    p.add_argument('--time', type=int, default=6, help='длительность каждого iperf3, с')
    p.add_argument('--repeat', type=int, default=5, help='кругов по всем экзитам и клиентам, берётся медиана')
    p.add_argument('--udp-rate', default='100M', help='скорость UDP-теста iperf3')
    p.add_argument('--dns', default='10.99.0.1', help='DNS-сервер в туннеле (AdGuard хаба)')
    p.add_argument('--dns-n', type=int, default=100, help='запросов в DNS-пачке за 1 с')
    p.add_argument('--ip-echo', default='https://api.ipify.org',
                   help='сервис «мой IP» для проверки экзита; пусто — не проверять')
    p.add_argument('--out', help='сохранить сырые результаты в JSON')
    p.add_argument('--compare', nargs=2, metavar=('A.json', 'B.json'),
                   help='только сравнить два сохранённых прогона')
    args = p.parse_args()
    if args.compare:
        sys.stdout.write(compare(*args.compare))
        return
    if not (args.hub and args.client and args.iperf):
        p.error('нужны --hub, --client и --iperf')
    res = run(args)
    if args.out:
        with open(args.out, 'w') as fh:
            json.dump(res, fh, indent=1)
    sys.stdout.write(render(res))


if __name__ == '__main__':
    main()
