#!/usr/bin/env bash
# Stage 20 — sing-box роутер на RU-ноде.
# Поднимает sing-box: трафик клиентов (WG/OpenVPN, заведённый стадиями 28/29
# в sing-box TPROXY :7898) роутится по geoip/geosite в hy2-туннели до
# exit-нод (см. gen-router-config.py). Идемпотентен.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${PUB_IP_OUT:?}" "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}" "${HY2_MGMT_PORT:?}" "${EXIT_TAGS:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 20-ru-router is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='20-ru-router'

mkdir -p /etc/anysda /etc/sing-box /var/lib/sing-box

# ----------------------------------------------------------------------------
# 1. sing-box install
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/6] sing-box"
SB_VER='1.10.3'
if [[ ! -x /usr/local/bin/sing-box ]] || ! /usr/local/bin/sing-box version 2>&1 | grep "$SB_VER" >/dev/null; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/sing-box-${SB_VER}-linux-amd64.tar.gz" \
    | tar -xz -C /tmp
  install -m0755 "/tmp/sing-box-${SB_VER}-linux-amd64/sing-box" /usr/local/bin/sing-box
  rm -rf "/tmp/sing-box-${SB_VER}-linux-amd64"
fi
/usr/local/bin/sing-box version | sed -n "1s/^/[$HOST_TAG]   /p"

# ----------------------------------------------------------------------------
# 2. GeoIP / Geosite DB
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/6] GeoIP/Geosite"
if [[ ! -f /var/lib/sing-box/geoip.db || $(find /var/lib/sing-box/geoip.db -mtime +7 2>/dev/null) ]]; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o /var/lib/sing-box/geoip.db   'https://github.com/SagerNet/sing-geoip/releases/latest/download/geoip.db'
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o /var/lib/sing-box/geosite.db 'https://github.com/SagerNet/sing-geosite/releases/latest/download/geosite.db'
fi
ls -la /var/lib/sing-box/{geoip,geosite}.db | sed "s/^/[$HOST_TAG]   /"

# ----------------------------------------------------------------------------
# 3. Secrets: clash-api на RU + пароли Hysteria2 с выходных нод
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [3/6] secrets"
gen_pwd() { openssl rand -base64 32 | tr -d '\n=' | head -c 32; }
[[ -f /etc/anysda/clash-secret.txt ]] || gen_pwd > /etc/anysda/clash-secret.txt
chmod 600 /etc/anysda/clash-secret.txt
RU_CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt)

FS=/tmp/anysda/foreign-secrets.env
[[ -f "$FS" ]] || { echo "[$HOST_TAG] $FS не найден — оркестратор должен был его загрузить"; exit 1; }
# Не source: значения пришли с экзитов. Берём только TAG_KEY=пароль в
# алфавите gen_pwd из 10-foreign, на остальном стадия падает.
while IFS= read -r _line || [[ -n "$_line" ]]; do
  [[ -z "$_line" ]] && continue
  if [[ ! "$_line" =~ ^([A-Z0-9]+_(DIRECT|WARP|OBFS|MGMT|CLASH))=([A-Za-z0-9+/]{16,128})$ ]]; then
    echo "[$HOST_TAG] в $FS чужая строка (${_line%%=*}) — экзит отдал не пароль, стадию прерываю"
    exit 1
  fi
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[3]}"
done < "$FS"

# Проверяем, что для каждой выходной ноды есть все нужные секреты
for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr a-z A-Z)
  eval ": \"\${${_T}_DIRECT:?секрет ${_t} DIRECT не найден в foreign-secrets.env}\""
  eval ": \"\${${_T}_WARP:?секрет ${_t} WARP не найден в foreign-secrets.env}\""
  eval ": \"\${${_T}_MGMT:?секрет ${_t} MGMT не найден в foreign-secrets.env}\""
  eval ": \"\${${_T}_OBFS:?секрет ${_t} OBFS не найден в foreign-secrets.env}\""
done

# ----------------------------------------------------------------------------
# 4. Генерация sing-box конфига через Python-скрипт (поддерживает N нод)
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/6] sing-box router config"
GEN=/tmp/anysda/gen-router-config.py
[[ -f "$GEN" ]] || { echo "[$HOST_TAG] $GEN не найден"; exit 1; }

WAN_IFACE=$(ip -4 -o route show default | awk '!f {print $5; f=1}')
: "${WAN_IFACE:?}"
# Backward-compat: gen-router-config.py читает WG_OUT_IFACE (имя из v1).
export WG_OUT_IFACE="$WAN_IFACE"

export RU_CLASH_SECRET EXIT_TAGS HY2_DIRECT_PORT HY2_WARP_PORT HY2_MGMT_PORT MGMT_IP
# YouTube (стадия 19-yt-zapret). `source` env-файла делает переменные
# ЛОКАЛЬНЫМИ для шелла — до python-генератора они без export не доезжают, и
# конфиг молча собирается без YouTube-правил (стадия при этом отрабатывает
# «успешно»). Дефолты — на случай env-файла, сгенерённого старым config2env.py.
export YT_ROUTE="${YT_ROUTE:-off}" YT_QUIC="${YT_QUIC:-block}" YT_MARK="${YT_MARK:-256}"
[[ -n "${YT_DOMAINS:-}" ]] && export YT_DOMAINS

for _t in $EXIT_TAGS; do
  _T=$(echo "$_t" | tr a-z A-Z)
  export "DOMAIN_${_T}"
  export "${_T}_DIRECT" "${_T}_WARP" "${_T}_MGMT" "${_T}_OBFS"
  # MGMT_IP_{T} задаёт mon-порт локального SOCKS-инбаунда (см. генератор).
  export "MGMT_IP_${_T}"
  eval "export ${_T}_HY2_DIRECT_PORT=\${${_T}_HY2_DIRECT_PORT:-${HY2_DIRECT_PORT}}"
  eval "export ${_T}_HY2_MGMT_PORT=\${${_T}_HY2_MGMT_PORT:-${HY2_MGMT_PORT}}"
done

# TLS pinning Hysteria2 outbound: если orchestrator push'нул cert экзита в
# /tmp/anysda/exit-certs/{tag}.pem — кладём его в /etc/sing-box/exit-certs
# и экспортируем {T}_HY2_CERT_PATH; gen-router-config.py подставит
# tls.certificate_path и снимет insecure. Backward-compat: если cert'а нет,
# RU-роутер продолжает работать с insecure=true (см. H5).
mkdir -p /etc/sing-box/exit-certs
chmod 700 /etc/sing-box/exit-certs
if [[ -d /tmp/anysda/exit-certs ]]; then
  for _t in $EXIT_TAGS; do
    _T=$(echo "$_t" | tr a-z A-Z)
    _src="/tmp/anysda/exit-certs/${_t}.pem"
    if [[ -f "$_src" ]]; then
      install -m 600 "$_src" "/etc/sing-box/exit-certs/${_t}.pem"
      eval "export ${_T}_HY2_CERT_PATH=/etc/sing-box/exit-certs/${_t}.pem"
    fi
  done
fi

# Write the BASE config (without manual routes). The manual-routes watcher
# derives /etc/sing-box/config.json = config-base.json + panel rules, so
# config-base.json MUST be refreshed on every run — a stale base silently
# drops inbounds added to gen-router-config.py since it was first
# snapshotted (this is how the wg/ovpn TPROXY :7898 inbound got lost).
python3 "$GEN" > /etc/sing-box/config-base.json
chmod 600 /etc/sing-box/config-base.json

if ! /usr/local/bin/sing-box check -c /etc/sing-box/config-base.json; then
  echo "[$HOST_TAG] конфиг невалиден — сервис не трогаю"
  exit 1
fi
# config.json (база + ручные маршруты панели) собирается в шаге 6, и sing-box
# перезапускается один раз уже на нём (VPN2-52).

# ----------------------------------------------------------------------------
# 5. systemd service для sing-box
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [5/6] sing-box service"

# Очистка legacy Shadowsocks/Outline — SS выпилен из проекта. Снимаем
# артефакты прошлых деплоев (стадия 27, anysda-iptables --uid-owner outline),
# если они ещё остались на ноде. Идемпотентно: на чистой ноде — no-op.
for _u in anysda-iptables outline-ss-server; do
  if [[ -f "/etc/systemd/system/${_u}.service" ]]; then
    echo "[$HOST_TAG]   снимаю legacy ${_u}.service"
    systemctl disable --now "$_u" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${_u}.service"
  fi
done
[[ -x /usr/local/sbin/anysda-iptables.sh ]] && /usr/local/sbin/anysda-iptables.sh down >/dev/null 2>&1 || true
rm -f /usr/local/sbin/anysda-iptables.sh /usr/local/bin/outline-ss-server
rm -rf /etc/outline-ss-server
id outline >/dev/null 2>&1 && userdel outline >/dev/null 2>&1 || true

cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box (anysda-vpn2 RU router)
After=network-online.target nss-lookup.target
Wants=network-online.target
# StartLimitIntervalSec=0 — роутер обязан подниматься ВСЕГДА: убираем лимит
# systemd «5 крашей за 10с → сдаться», иначе при краш-цикле весь VPN ляжет.
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
# Restart=always (а не on-failure): роутер обязан подниматься после ЛЮБОЙ
# остановки, включая чистый SIGTERM — иначе случайный `systemctl stop`/`kill`
# (или pkill по имени) кладёт маршрутизацию насовсем.
Restart=always
RestartSec=2s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable sing-box >/dev/null 2>&1

# clash-api слушает 10.99.0.1:9090 — это lo-алиас на самой entry, наружу не
# торчит. Снимаем legacy mesh-правило ufw (mesh снят, служебный трафик ушёл на
# Hysteria2, см. стадию 05 и docs/mgmt-over-hysteria2-design.md).
ufw delete allow proto tcp from 10.99.0.0/24 to any port 9090 >/dev/null 2>&1 || true

# route_localnet — нужен TPROXY WG/OpenVPN (--on-ip 127.0.0.1, стадии 28/29).
cat > /etc/sysctl.d/99-anysda-vpn.conf <<'EOF'
net.ipv4.conf.all.route_localnet=1
EOF
sysctl -p /etc/sysctl.d/99-anysda-vpn.conf >/dev/null

# ----------------------------------------------------------------------------
# 6. Sing-box manual routes: host-side apply script + systemd.path watcher
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [6/6] sing-box manual-routes watcher"

cat > /usr/local/sbin/anysda-apply-routes.py << 'PYEOF'
#!/usr/bin/env python3
import ipaddress, json, os, subprocess, sys, tempfile

# --no-restart: только собрать config.json (стадия 20 перезапускает sing-box
# сама, один раз на итоговом конфиге).
NO_RESTART = '--no-restart' in sys.argv[1:]

MANUAL_ROUTES = '/etc/anysda/manual-routes.json'
CLIENT_PREFS  = '/etc/anysda/client-prefs.json'
SB_CONFIG     = '/etc/sing-box/config.json'
SB_CONFIG_BASE= '/etc/sing-box/config-base.json'
RULES_DIR     = '/etc/sing-box/manual-routes'
PREFS_DIR     = '/etc/sing-box/client-prefs'
SB_BIN        = '/usr/local/bin/sing-box'

if not os.path.exists(SB_CONFIG_BASE) and os.path.exists(SB_CONFIG):
    import shutil
    shutil.copy2(SB_CONFIG, SB_CONFIG_BASE)

if not os.path.exists(SB_CONFIG_BASE):
    print('config-base.json missing', file=sys.stderr); sys.exit(1)

try:
    with open(MANUAL_ROUTES) as f: rows = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    rows = []

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

# Ручные маршруты — сразу за служебной головой списка (mon-*, блокировки
# loopback и 853, выход бота), но выше всего остального (YouTube, .ru,
# дорожки). Выше головы маршрут панели на домен или подсеть перехватывал бы
# скрейп экзитов и бота, а 0.0.0.0/0 на экзит открывал бы его loopback.
route = cfg['route']
route['rule_set'] = route.get('rule_set', []) + [
    {'type': 'local', 'tag': f'manual-{t}', 'format': 'source', 'path': rules_path(t)}
    for t in targets]
rules = route['rules']
head = 0
while head < len(rules) and ('inbound' in rules[head] or rules[head].get('outbound') == 'block-out'):
    head += 1
route['rules'] = rules[:head] + [{'rule_set': [f'manual-{t}'], 'outbound': t} for t in targets] + rules[head:]

# Предпочитаемые экзиты клиентов (панель пишет client-prefs.json:
# {"<узел>": ["<IP устройства>", ...]}). IP узла — в набор client-pref-<узел>
# с выходом pin-hy2-<узел>-direct: пока узел выключен, мёртв или в штрафной,
# вотчдог держит pin на foreign-best. Наборы заведены на каждый узел, даже
# пустые, и перечитываются на ходу: смена предпочтения не трогает config.json
# и не рвёт соединения. Правила — перед дорожками, то есть после ручных
# маршрутов, YouTube и РФ: меняется только то, что ушло бы в дорожку.
try:
    with open(CLIENT_PREFS) as f: prefs = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    prefs = {}
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

def put(path, body, check):
    """Атомарно (tmp + rename в том же каталоге) заменить файл, если изменился."""
    if read(path) == body:
        return False
    with tempfile.NamedTemporaryFile('w', dir=os.path.dirname(path), delete=False, suffix='.tmp') as tmp:
        tmp.write(body); tmp_path = tmp.name
    try:
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

summary = (f'{len(manual)} manual route(s), '
           f'{sum(map(len, pref_sets.values()))} IP с предпочтением, наборов изменено: {len(changed)}')
if read(SB_CONFIG) == new_body:
    tail = 'config.json уже собран' if NO_RESTART else 'sing-box перечитает их сам, без перезапуска'
    print(f'{summary}, {tail}')
    sys.exit(0)

# config.json меняется только при смене выходов в базе (передеплой) — тогда
# без перезапуска не обойтись.
put(SB_CONFIG, new_body, [SB_BIN, 'check', '-c'])
if NO_RESTART:
    print(f'{summary}, config.json собран')
    sys.exit(0)
subprocess.run(['systemctl', 'restart', 'sing-box'], check=True)
print(f'{summary}, config.json изменился, sing-box restarted')
PYEOF
chmod +x /usr/local/sbin/anysda-apply-routes.py

cat > /etc/systemd/system/anysda-apply-routes.service << 'EOF'
[Unit]
Description=anysda-vpn2 — apply manual sing-box routes from panel
After=sing-box.service
# Дефолтный лимит (5 стартов за 10с) выбивался одной правкой в панели: она
# пишет manual-routes.json несколько раз подряд, .path триггерит нас на каждую
# запись, и юнит навсегда уходил в failed (start-limit-hit) — правки маршрутов
# переставали применяться молча. Скрипт идемпотентен и при отсутствии изменений
# не трогает sing-box, поэтому лимит здесь не нужен.
StartLimitIntervalSec=0

[Service]
Type=oneshot
# Дебаунс: пока мы спим, повторные срабатывания .path схлопываются systemd'ом
# в одно отложенное задание вместо пачки отдельных запусков.
ExecStartPre=/bin/sleep 2
ExecStart=/usr/local/sbin/anysda-apply-routes.py
EOF

cat > /etc/systemd/system/anysda-apply-routes.path << 'EOF'
[Unit]
Description=Watch /etc/anysda/manual-routes.json and client-prefs.json for changes

[Path]
PathModified=/etc/anysda/manual-routes.json
PathModified=/etc/anysda/client-prefs.json
Unit=anysda-apply-routes.service

[Install]
WantedBy=multi-user.target
EOF

# Touch empty file so the path-watcher is happy on first boot
[[ -f /etc/anysda/manual-routes.json ]] || echo '[]' > /etc/anysda/manual-routes.json
[[ -f /etc/anysda/client-prefs.json ]] || echo '{}' > /etc/anysda/client-prefs.json

systemctl daemon-reload
systemctl enable anysda-apply-routes.path >/dev/null 2>&1
systemctl start  anysda-apply-routes.path

# config.json = свежая config-base.json + ручные маршруты панели, затем ОДИН
# рестарт sing-box уже на итоговом конфиге (раньше было два: на голой базе,
# которая на секунды сносила ручные маршруты, и ещё один из apply-routes).
if ! /usr/local/sbin/anysda-apply-routes.py --no-restart 2>&1 | sed "s/^/[$HOST_TAG]   /"; then
  echo "[$HOST_TAG]   ⚠ ручные маршруты не применились — sing-box поднимаю на базовом конфиге"
  install -m 600 /etc/sing-box/config-base.json /etc/sing-box/config.json
fi
# Перезапуск рвёт все соединения клиентов, поэтому только если sing-box читает
# уже не то, с чем стартовал: отпечаток бинарника, юнита, конфига, сертификатов
# экзитов и geo-баз сверяем с записанным при прошлом старте. Наборы ручных
# маршрутов не считаем: их sing-box перечитывает сам.
SB_FP_FILE=/var/lib/sing-box/applied.sha256
sb_fp=$({ cat /usr/local/bin/sing-box /etc/systemd/system/sing-box.service \
  /etc/sing-box/config.json /etc/sing-box/exit-certs/*.pem \
  /var/lib/sing-box/geoip.db /var/lib/sing-box/geosite.db 2>/dev/null || true; } \
  | sha256sum | cut -d' ' -f1)
if systemctl is-active --quiet sing-box && [[ "$(cat "$SB_FP_FILE" 2>/dev/null)" == "$sb_fp" ]]; then
  echo "[$HOST_TAG]   конфиг, сертификаты и бинарник прежние — sing-box не перезапускаю"
else
  systemctl restart sing-box
  echo "$sb_fp" > "$SB_FP_FILE"
  sleep 2
fi
systemctl status sing-box --no-pager -n 4 | sed -n "1,6s/^/[$HOST_TAG]   /p"

# ----------------------------------------------------------------------------
# ssh <tag> на экзиты через служебный hy2-туннель (VPN2-38). nc идёт в
# локальный SOCKS-инбаунд mon-{tag} (порт = 10100 + последний октет
# MGMT_IP_{T}, как в gen-router-config.py), туннель приводит на 127.0.0.1:22
# экзита. Ключ entry (/root/.ssh/id_ed25519_mgmt) раскладывает 00-bootstrap.
# Адрес у всех экзитов 127.0.0.1, поэтому хост-ключ сверяем по псевдониму
# anysda-<tag>; known_hosts собирается заново с ключей, которые deploy.sh
# снял с экзитов, — переустановленный экзит не упрётся в старый ключ.
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] ssh-конфиг на экзиты через служебный туннель"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
SSH_MGMT_CONF=/root/.ssh/anysda-mgmt.conf
SSH_MGMT_KNOWN=/root/.ssh/known_hosts_mgmt
{
  echo "# anysda-vpn — генерирует infra/scripts/20-ru-router.sh, руками не править"
  for _t in $EXIT_TAGS; do
    _T=$(echo "$_t" | tr a-z A-Z)
    _var="MGMT_IP_${_T}"
    _ip="${!_var:-}"
    if [[ -z "$_ip" ]]; then
      echo "[$HOST_TAG]   ВНИМАНИЕ: MGMT_IP_${_T} не задан — ssh ${_t} не настроен" >&2
      continue
    fi
    cat <<EOF

Host ${_t}
  HostName 127.0.0.1
  Port 22
  User root
  IdentityFile /root/.ssh/id_ed25519_mgmt
  IdentitiesOnly yes
  HostKeyAlias anysda-${_t}
  UserKnownHostsFile ${SSH_MGMT_KNOWN}
  StrictHostKeyChecking accept-new
  ServerAliveInterval 15
  ProxyCommand nc -X 5 -x 127.0.0.1:$(( 10100 + ${_ip##*.} )) %h %p
EOF
  done
} > "$SSH_MGMT_CONF"
chmod 600 "$SSH_MGMT_CONF"

: > "$SSH_MGMT_KNOWN"
for _t in $EXIT_TAGS; do
  _hk="/tmp/anysda/exit-hostkeys/${_t}.pub"
  if [[ -s "$_hk" ]]; then
    awk -v h="anysda-${_t}" 'NF >= 2 {print h, $1, $2; exit}' "$_hk" >> "$SSH_MGMT_KNOWN"
  fi
done
chmod 600 "$SSH_MGMT_KNOWN"

touch /root/.ssh/config
chmod 600 /root/.ssh/config
if ! grep -qxF "Include ${SSH_MGMT_CONF}" /root/.ssh/config; then
  # Include обязан стоять до первого Host, иначе попадёт внутрь его блока.
  { echo "Include ${SSH_MGMT_CONF}"; cat /root/.ssh/config; } > /root/.ssh/config.new
  mv /root/.ssh/config.new /root/.ssh/config
  chmod 600 /root/.ssh/config
fi
echo "[$HOST_TAG]   хосты: $(awk '$1 == "Host" {printf "%s ", $2}' "$SSH_MGMT_CONF")"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
