#!/usr/bin/env bash
# Приёмка аварийного режима (VPN2-429) на живом стенде: entry и 2+ экзита
# уже развёрнуты штатным ./deploy.sh. Каждая команда - один или несколько
# сценариев ТЗ; итог строками "PASS"/"FAIL", код возврата 1 при любом FAIL.
#
#   emergency-accept.sh -n nodes.txt -e <тег entry> [-i ключ] [-k known_hosts] <команда>
#
# nodes.txt: строка на узел, тег узла стоит перед его IPv4, поля после
# адреса не читаются (там бывает пароль).
#
#   status                   роли экзитов, порт Hysteria2, очередь Telegram
#   quiet <экзит> [мин]      entry не слышит экзит N мин (21): аварии нет        1, 2
#   start                    стоп агента entry, ждать лидера (до 25 мин)         3, 4
#   deliveries               строки очереди Telegram у лидера, повторов нет      3, 6, 10, 11
#   hold <мин>               авария держится, роль и выдача не меняются           26
#   restart-leader           рестарт агента лидера: та же выдача, без повторов    6
#   kill-leader              стоп агента лидера: новый лидер, свой пароль         7, 25
#   split <экзит>            экзит отрезан от соседей: автономия, затем слияние   8, 9
#   restart-exits            одновременный старт агентов всех экзитов             5
#   telegram-block <мин>     Telegram недоступен у экзитов: повтор с паузой       11
#   sessions-blind <сек>     API сессий Hysteria2 закрыт: статус неизвестен       30
#   entry-not-ready <мин>    агент entry при остановленном sing-box               12
#   recover                  вернуть entry, дождаться фиксации срока              13
#   recover-cancel           entry снова падает до срока: отмена                  14
#   finish                   дождаться конца, порт закрыт, все observer           15
#   tunnels                  штатные туннели entry к экзитам отвечают             15
#   password [N]             N паролей панели: 30 знаков, все группы              24
#   client up <id>|probe|down  hy2-клиент на entry профилем устройства лидера     20, 26, 28, 29
#   client flow <сек>        держать поток через клиента (индикатор «Подключён»)  28
#   indicator [текст]        вход в панель лидера, /api/state без секретов; с текстом - рассылка  10, 11, 27-30
#
# Сетевые сценарии ставят nft-таблицу inet vpn2_accept на узлах и снимают её
# trap-ом при любом выходе.
set -Eeuo pipefail

NODES_FILE=''
ENTRY_TAG=''
SSH_KEY=''
KNOWN_HOSTS=''
STATE=/var/lib/vpn2-emergency
TABLE=vpn2_accept
FAILED=0
declare -A IP=()
EXITS=()

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAILED=1; }
die() { printf 'ошибка: %s\n' "$*" >&2; exit 2; }

while getopts 'n:e:i:k:h' opt; do
  case $opt in
    n) NODES_FILE=$OPTARG ;;
    e) ENTRY_TAG=$OPTARG ;;
    i) SSH_KEY=$OPTARG ;;
    k) KNOWN_HOSTS=$OPTARG ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))
[[ $# -ge 1 && -r "$NODES_FILE" && -n "$ENTRY_TAG" ]] || usage

while read -r -a fields; do
  for ((i = 1; i < ${#fields[@]}; i++)); do
    if [[ ${fields[i]} =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      IP[${fields[i - 1]}]=${fields[i]}
      [[ ${fields[i - 1]} == "$ENTRY_TAG" ]] || EXITS+=("${fields[i - 1]}")
      break
    fi
  done
done < "$NODES_FILE"
[[ -n "${IP[$ENTRY_TAG]:-}" && ${#EXITS[@]} -ge 2 ]] || die "в $NODES_FILE нет entry $ENTRY_TAG или двух экзитов"

node() {
  local tag=$1
  shift
  local opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o LogLevel=ERROR)
  [[ -z "$SSH_KEY" ]] || opts+=(-i "$SSH_KEY")
  [[ -z "$KNOWN_HOSTS" ]] || opts+=(-o "UserKnownHostsFile=$KNOWN_HOSTS")
  # Текст из .bashrc узла приходит в stdout раньше вывода команды: команда
  # сначала печатает метку, всё до неё отбрасывается.
  ssh "${opts[@]}" "root@${IP[$tag]}" "printf '\\n%s\\n' vpn2-ssh-output-begin;" "$@" \
    | sed -u '0,/^vpn2-ssh-output-begin$/d'
  return "${PIPESTATUS[0]}"
}

# Состояние агента экзита одной строкой: роль поколение active autonomous
# hy2-порт-слушает emergency_id(8) recovery-раунд срок; роль stopped - агент не запущен.
leader_line() {
  node "$1" "python3 - $STATE" <<'PY'
import json, subprocess, sys
d = json.load(open(sys.argv[1] + '/agent.json')).get('leader', {})
# agent.json остаётся после остановки агента: роль из него тогда устарела.
if subprocess.run(['systemctl', 'is-active', '-q', 'vpn2-emergency']).returncode:
    d = dict(d, role='stopped')
r = d.get('recovery') or {}
hy2 = json.load(open('/etc/vpn2/emergency-hy2.json'))['port']
port = 'open' if subprocess.run(['ss', '-Hlun', f'sport = :{hy2}'], capture_output=True,
                                text=True).stdout.strip() else 'closed'
print(d.get('role', 'observer'), d.get('generation'), str(d.get('active')).lower(),
      str(bool(d.get('autonomous'))).lower(), port, (d.get('emergency_id') or '-')[:8],
      r.get('round', '-'), r.get('deadline') or '-')
PY
}

status() {
  local tag
  for tag in "${EXITS[@]}"; do
    printf '%-6s %s\n' "$tag" "$(leader_line "$tag" 2>&1 || echo 'нет ответа')"
  done
  printf '%-6s агент %s\n' "$ENTRY_TAG" "$(node "$ENTRY_TAG" systemctl is-active vpn2-emergency || true)"
}

# Теги экзитов в роли leader, через пробел.
leaders() {
  local tag out=()
  for tag in "${EXITS[@]}"; do
    [[ $(leader_line "$tag" 2>/dev/null | cut -d' ' -f1) == leader ]] && out+=("$tag")
  done
  echo "${out[*]}"
}

# Ждать, пока лидеров ровно один и его выдача готова; печатает тег.
wait_leader() {
  local limit=$1 except=${2:-} started=$SECONDS current
  while ((SECONDS - started < limit)); do
    current=$(leaders)
    if [[ -n "$current" && "$current" != *' '* && "$current" != "$except" ]] \
        && node "$current" "journalctl -u vpn2-emergency --since '-${limit}s' -o cat | grep -q 'emergency access issued'"; then
      echo "$current"
      return 0
    fi
    sleep 10
  done
  return 1
}

journal() {
  local tag=$1 since=$2 pattern=$3
  node "$tag" "journalctl -u vpn2-emergency --since '$since' -o short-iso --no-pager | grep -E '$pattern' || true"
}

firewall() {
  local tag=$1 rules=$2
  node "$tag" "nft -f -" <<EOF
table inet $TABLE
delete table inet $TABLE
table inet $TABLE {
$rules
}
EOF
}

firewall_clear() {
  local tag
  for tag in "$ENTRY_TAG" "${EXITS[@]}"; do
    node "$tag" "nft delete table inet $TABLE 2>/dev/null; true" || true
  done
}

# Очередь Telegram лидера: строки delivery без текстов и чатов.
deliveries() {
  local tag
  tag=${1:-$(leaders | cut -d' ' -f1)}
  [[ -n "$tag" ]] || { fail 'лидера нет'; return; }
  log "очередь Telegram на $tag"
  node "$tag" "python3 - $STATE" <<'PY'
import collections, sqlite3, sys
current = None
import json
current = json.load(open(sys.argv[1] + '/agent.json'))['leader'].get('emergency_id')
dupes = 0
for audience in ('admin', 'client'):
    db = sqlite3.connect(f'file:{sys.argv[1]}/telegram/{audience}.sqlite3?mode=ro', uri=True)
    rows = db.execute('SELECT emergency,generation,client,event,state,reason,attempts,'
                      '(SELECT count(*) FROM part WHERE delivery=delivery.id AND sent=1),'
                      '(SELECT count(*) FROM part WHERE delivery=delivery.id) FROM delivery ORDER BY id').fetchall()
    count = collections.Counter()
    for e, g, c, ev, st, reason, attempts, sent, total in rows:
        mark = '*' if e == current else ' '
        print(f'{mark} {audience:6} gen={g} client={c} {ev:16} {st:9} parts={sent}/{total} attempts={attempts} {reason}')
        if e == current and ev in ('initial', 'leader_changed'):
            count[(g, c, ev)] += 1
    dupes += sum(n - 1 for n in count.values())
print('DUPLICATES', dupes)
PY
}

no_duplicates() {
  local out
  out=$(deliveries "$2")
  printf '%s\n' "$out"
  if [[ $out == *$'\nDUPLICATES 0'* ]]; then pass "$1 повторов выдачи нет"; else fail "$1 повторы выдачи"; fi
}

cmd_quiet() {
  local cut=$1 minutes=${2:-21} tag line
  [[ " ${EXITS[*]} " == *" $cut "* ]] || die "экзит $cut не найден"
  [[ $(node "$ENTRY_TAG" systemctl is-active vpn2-emergency) == active ]] || die 'агент entry не запущен'
  [[ -z "$(leaders)" ]] || die 'авария уже идёт'
  trap firewall_clear EXIT
  firewall "$ENTRY_TAG" "  chain i { type filter hook input priority -10; ip saddr ${IP[$cut]} udp dport 51821 drop; }
  chain o { type filter hook output priority -10; ip daddr ${IP[$cut]} udp sport 51821 drop; }"
  log "entry не слышит $cut $minutes мин"
  for ((i = 1; i <= minutes; i++)); do
    sleep 60
    for tag in "${EXITS[@]}"; do
      line=$(leader_line "$tag")
      [[ $(cut -d' ' -f3 <<< "$line") == false ]] || { fail "минута $i: $tag $line"; break 2; }
    done
  done
  firewall_clear
  trap - EXIT
  for tag in "${EXITS[@]}"; do
    line=$(leader_line "$tag")
    if [[ $(cut -d' ' -f3 <<< "$line") == false ]]; then
      pass "S1/S2 $tag через $minutes мин: $line"
    else
      fail "S1/S2 $tag: $line"
    fi
  done
}

cmd_start() {
  [[ -z "$(leaders)" ]] || die 'авария уже идёт'
  local since leader
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  node "$ENTRY_TAG" systemctl stop vpn2-emergency
  log "агент entry остановлен ($since UTC), ждём лидера до 25 мин"
  if leader=$(wait_leader 1500); then
    pass "S3 лидер $leader через $((SECONDS / 60)) мин"
  else
    fail "S3 лидера нет за 25 мин: $(leaders)"
    return
  fi
  status
  local tag
  for tag in "${EXITS[@]}"; do
    journal "$tag" "$since" 'role=|emergency access issued'
  done
  deliveries "$leader"
}

cmd_hold() {
  local minutes=$1 first line tag probe=''
  first=$(leaders)
  [[ -n "$first" && "$first" != *' '* ]] || die "нужен один лидер, есть: '$first'"
  local before
  before=$(leader_line "$first")
  log "держим аварию $minutes мин, лидер $first: $before"
  for ((i = 1; i <= minutes; i++)); do
    sleep 60
    line=$(leader_line "$first")
    if [[ $line != "$before" ]]; then
      fail "S26 минута $i: $first было '$before', стало '$line'"
      return
    fi
    if node "$ENTRY_TAG" "test -f $CLIENT/client.pid"; then
      probe=$(cmd_client probe)
      [[ $probe == ip=* && $probe != 'ip=нет ответа' ]] || fail "S26 минута $i: клиент hy2 $probe"
    fi
    ((i % 10)) || log "минута $i: $line ${probe:-}"
  done
  for tag in "${EXITS[@]}"; do
    journal "$tag" "-$((minutes + 1))min" 'emergency ended|emergency end|role='
  done
  pass "S26 $minutes мин без entry: $first $line"
}

panel_password_hash() {
  node "$1" "python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(\"digest\",\"-\")[:12], d.get(\"generation\"))' $STATE/web-auth.json 2>/dev/null || echo '- -'"
}

cmd_restart_leader() {
  local leader before after since
  leader=$(leaders)
  [[ -n "$leader" && "$leader" != *' '* ]] || die "нужен один лидер, есть: '$leader'"
  before="$(leader_line "$leader") $(panel_password_hash "$leader")"
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  node "$leader" systemctl restart vpn2-emergency
  sleep 45
  after="$(leader_line "$leader") $(panel_password_hash "$leader")"
  log "до:    $before"
  log "после: $after"
  journal "$leader" "$since" 'role=|issued|failed'
  if [[ $before == "$after" ]]; then pass "S6 $leader: роль, поколение и пароль те же"; else fail 'S6 состояние изменилось'; fi
  no_duplicates S6 "$leader"
}

cmd_kill_leader() {
  local old new since
  old=$(leaders)
  [[ -n "$old" && "$old" != *' '* ]] || die "нужен один лидер, есть: '$old'"
  local old_hash
  old_hash=$(panel_password_hash "$old")
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  node "$old" systemctl stop vpn2-emergency
  local started=$SECONDS
  if new=$(wait_leader 300 "$old"); then
    pass "S7 новый лидер $new через $((SECONDS - started)) с"
  else
    fail "S7 нового лидера нет за 300 с"
    node "$old" systemctl start vpn2-emergency
    return
  fi
  local new_hash
  new_hash=$(panel_password_hash "$new")
  log "пароль панели $old: $old_hash, $new: $new_hash (первые знаки sha256)"
  [[ ${new_hash%% *} != "${old_hash%% *}" ]] && pass 'S25 у нового лидера свой пароль' || fail 'S25 пароль тот же'
  node "$old" systemctl start vpn2-emergency
  # Прежние реквизиты живут переходный срок (TRANSITION, 600 с) от готовности нового лидера.
  started=$SECONDS
  until [[ $(leader_line "$old") == *' closed '* ]] || ((SECONDS - started > 720)); do sleep 15; done
  [[ $(leader_line "$old") == *' closed '* ]] && pass "S7 $old закрыл прежний Hysteria2 через $((SECONDS - started)) с после старта" \
    || fail "S7 $old держит прежний Hysteria2 дольше 720 с"
  journal "$old" "$since" 'role=|end'
  journal "$new" "$since" 'role=|issued'
  deliveries "$new"
}

cmd_split() {
  local cut=$1 others=() tag line
  for tag in "${EXITS[@]}"; do [[ $tag == "$cut" ]] || others+=("${IP[$tag]}"); done
  local set
  set=$(IFS=,; echo "${others[*]}")
  trap firewall_clear EXIT
  local since
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  firewall "$cut" "  chain i { type filter hook input priority -10; ip saddr { $set } udp dport 51821 drop; }
  chain o { type filter hook output priority -10; ip daddr { $set } udp dport 51821 drop; }"
  log "$cut отрезан от соседей"
  local started=$SECONDS auto=''
  while ((SECONDS - started < 600)); do
    sleep 15
    line=$(leader_line "$cut")
    if [[ $line == 'leader '*' true true '* ]]; then auto=$((SECONDS - started)); break; fi
  done
  if [[ -n "$auto" ]]; then pass "S8 $cut автономен через $auto с: $line"; else fail "S8 автономии нет: $line"; fi
  journal "$cut" "$since" 'role=|held|issued'
  status
  log "панель $cut:"
  PANEL_TAG=$cut cmd_indicator | tail -1
  firewall_clear
  trap - EXIT
  log 'связь восстановлена'
  sleep 120
  status
  for tag in "${EXITS[@]}"; do journal "$tag" "$since" 'role=|step|issued|ended'; done
  line=$(leaders)
  [[ -n "$line" && "$line" != *' '* ]] && pass "S9 после слияния один лидер: $line" || fail "S9 лидеры: '$line'"
}

cmd_restart_exits() {
  local since tag
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  for tag in "${EXITS[@]}"; do node "$tag" systemctl restart vpn2-emergency & done
  wait
  sleep 120
  status
  for tag in "${EXITS[@]}"; do journal "$tag" "$since" 'role=|issued'; done
  local line
  line=$(leaders)
  [[ -n "$line" && "$line" != *' '* ]] && pass "S5 один лидер: $line" || fail "S5 лидеры: '$line'"
  no_duplicates S5 "$line"
}

TELEGRAM4='149.154.160.0/20, 91.108.0.0/16, 95.161.64.0/20, 185.76.151.0/24'
TELEGRAM6='2001:67c:4e8::/48, 2001:b28:f23d::/48, 2001:b28:f23f::/48, 2a0a:f280::/32'

cmd_telegram_block() {
  local minutes=$1 tag
  trap firewall_clear EXIT
  for tag in "${EXITS[@]}"; do
    firewall "$tag" "  chain o { type filter hook output priority -10; ip daddr { $TELEGRAM4 } reject; ip6 daddr { $TELEGRAM6 } reject; }"
  done
  log "Telegram закрыт на экзитах $minutes мин"
  cmd_indicator 'Проверка рассылки: Telegram был недоступен' | grep -E 'рассылка|панель|delivery'
  sleep $((minutes * 60))
  deliveries
  firewall_clear
  trap - EXIT
  log 'Telegram открыт, ждём повтор очереди'
  sleep 90
  deliveries
}

cmd_sessions_blind() {
  local seconds=$1 leader
  leader=$(leaders)
  trap firewall_clear EXIT
  firewall "$leader" "  chain o { type filter hook output priority -10; ip daddr 127.0.0.1 tcp dport 18443 meta skuid vpn2-emergency reject with tcp reset; }"
  log "API сессий Hysteria2 на $leader закрыт $seconds с"
  sleep "$seconds"
  node "$leader" "cat $STATE/hy2/sessions.json; echo"
  cmd_indicator
  firewall_clear
  trap - EXIT
}

cmd_entry_not_ready() {
  local minutes=$1 since leader
  leader=$(leaders)
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  node "$ENTRY_TAG" 'systemctl stop sing-box && systemctl start vpn2-emergency'
  log "агент entry запущен при остановленном sing-box, $minutes мин"
  sleep $((minutes * 60))
  status
  journal "$leader" "$since" 'recover|role=|ended'
  local line
  line=$(leader_line "$leader")
  [[ $line == leader*' true '*' open '* ]] && pass "S12 авария идёт: $line" || fail "S12 $line"
  node "$ENTRY_TAG" 'systemctl start sing-box'
}

cmd_recover() {
  local since leader
  leader=$(leaders)
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  node "$ENTRY_TAG" 'systemctl start sing-box vpn2-emergency'
  log 'entry поднят, ждём фиксации срока до 6 мин'
  local started=$SECONDS line=''
  while ((SECONDS - started < 360)); do
    sleep 15
    line=$(journal "$leader" "$since" 'deadline fixed')
    [[ -z "$line" ]] || break
  done
  journal "$leader" "$since" 'recover|deadline'
  [[ -n "$line" ]] && pass "S13 срок зафиксирован: $(leader_line "$leader")" || fail 'S13 срока нет'
  deliveries "$leader"
}

cmd_recover_cancel() {
  local since leader
  leader=$(leaders)
  since=$(date -u '+%Y-%m-%d %H:%M:%S')
  node "$ENTRY_TAG" systemctl stop vpn2-emergency
  log 'агент entry снова остановлен, ждём отмены до 3 мин'
  sleep 180
  journal "$leader" "$since" 'cancel|ended|role='
  local line
  line=$(leader_line "$leader")
  [[ $line == leader*' open '*' - -' ]] && pass "S14 возврат отменён, доступ держится: $line" || fail "S14 $line"
  deliveries "$leader"
}

cmd_finish() {
  local started=$SECONDS tag left
  log 'ждём конца аварии до 15 мин'
  while ((SECONDS - started < 900)); do
    sleep 30
    left=0
    for tag in "${EXITS[@]}"; do
      [[ $(leader_line "$tag" | cut -d' ' -f3,5) == 'false closed' ]] || left=1
    done
    ((left)) || break
  done
  status
  for tag in "${EXITS[@]}"; do journal "$tag" "-16min" 'ended|emergency end'; done
  ((left)) && fail 'S15 авария не закончилась' || pass 'S15 все экзиты observer, порт 18443 закрыт'
  if node "$ENTRY_TAG" "[ -f $CLIENT/client.pid ]"; then
    [[ $(cmd_client probe) == 'ip=нет ответа' ]] && pass 'S15 старый конфиг не подключается' \
      || fail 'S15 старый конфиг ещё работает'
  fi
  normal_tunnels
}

# Штатные туннели entry к экзитам: замер задержки через clash API sing-box.
normal_tunnels() {
  local out
  out=$(node "$ENTRY_TAG" 'python3 -' <<'PY'
import json, urllib.request
c = json.load(open('/etc/sing-box/config.json'))
api = c['experimental']['clash_api']
headers = {'Authorization': 'Bearer ' + api['secret']} if api.get('secret') else {}
for o in c['outbounds']:
    if o['type'] != 'trojan':
        continue
    url = (f"http://{api['external_controller']}/proxies/{o['tag']}/delay"
           '?url=https://www.gstatic.com/generate_204&timeout=8000')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=12) as r:
            print(o['tag'], json.load(r)['delay'], 'ms')
    except OSError as e:
        print(o['tag'], 'FAIL', e)
PY
)
  printf '%s\n' "$out"
  [[ -n $out && $out != *FAIL* ]] && pass 'штатные туннели entry к экзитам работают' \
    || fail 'штатный туннель не отвечает'
}

CLIENT=/run/vpn2-accept
SOCKS=127.0.0.1:17890

# Клиент Hysteria2 на entry: sing-box с socks-входом и профилем устройства
# из выдачи лидера. Ссылка идёт с узла на узел через этот скрипт, не печатается.
cmd_client() {
  local action=$1 leader uri
  case $action in
    up)
      leader=$(leaders)
      uri=$(node "$leader" "python3 - $STATE $2" <<'PY'
import json, sys
state = json.load(open(sys.argv[1] + '/hy2/generation.json'))
print(next(d['uri'] for d in state['devices'] if d['id'] == int(sys.argv[2])))
PY
)
      node "$ENTRY_TAG" "mkdir -m 700 -p $CLIENT && python3 - $CLIENT ${SOCKS##*:} && cd $CLIENT \
        && { setsid nohup sing-box run -c client.json > client.log 2>&1 < /dev/null & echo \$! > client.pid; }" <<PY
import json, sys
from urllib.parse import urlsplit, parse_qs, unquote
u = urlsplit('''$uri''')
q = parse_qs(u.query)
out = {'type': 'hysteria2', 'tag': 'hy2', 'server': u.hostname, 'server_port': u.port,
       'password': unquote(u.username), 'tls': {'enabled': True, 'server_name': q['sni'][0]}}
config = {'log': {'level': 'warn'},
          'inbounds': [{'type': 'socks', 'listen': '127.0.0.1', 'listen_port': int(sys.argv[2])}],
          'outbounds': [out]}
with open(sys.argv[1] + '/client.json', 'w') as f:
    json.dump(config, f)
PY
      sleep 3
      log "клиент на entry: устройство $2 у $leader"
      cmd_client probe
      ;;
    probe)
      node "$ENTRY_TAG" "curl -sS -m 15 --socks5-hostname $SOCKS https://cloudflare.com/cdn-cgi/trace | grep '^ip=' || echo 'ip=нет ответа'"
      ;;
    flow)
      # Обрыв по -m (код 28) и есть успех: поток держался всё время.
      node "$ENTRY_TAG" "curl -s -o /dev/null -w '%{http_code} %{size_download}\\n' -m $2 --limit-rate 2k --socks5-hostname $SOCKS https://fsn1-speed.hetzner.com/100MB.bin; [ \$? -eq 28 ]" || fail "поток через клиента оборвался раньше $2 с"
      ;;
    down)
      node "$ENTRY_TAG" "[ -f $CLIENT/client.pid ] && kill \$(cat $CLIENT/client.pid); rm -f $CLIENT/client.pid $CLIENT/client.json; true"
      ;;
    *) usage ;;
  esac
}

cmd_indicator() {
  local leader text
  leader=${PANEL_TAG:-$(leaders)}
  text=$(printf %q "${1:-}")
  node "$leader" "python3 - $STATE ${IP[$leader]} $text" <<'PY'
import json, ssl, sys, time, urllib.request
base = f'https://{sys.argv[2]}'
password = json.load(open(sys.argv[1] + '/web-auth.json'))['password']
headers = {'Origin': base, 'Content-Type': 'application/json'}
ctx = ssl.create_default_context()
req = urllib.request.Request(base + '/api/login', json.dumps({'password': password}).encode(), headers)
with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
    cookie = r.headers['Set-Cookie'].split(';', 1)[0]
if sys.argv[3:] and sys.argv[3]:
    started = time.monotonic()
    req = urllib.request.Request(base + '/api/broadcast', json.dumps({'text': sys.argv[3]}).encode(),
                                 headers | {'Cookie': cookie})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        print(f'рассылка: {r.status} за {time.monotonic() - started:.2f} с')
started = time.monotonic()
req = urllib.request.Request(base + '/api/state', headers=headers | {'Cookie': cookie})
with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
    state = json.load(r)
print(f'панель ответила за {time.monotonic() - started:.2f} с')
for c in state['clients']:
    print(f"{c['name']:14} status={c['status']:8} allowed={c['allowed']} telegram={c['telegram']} "
          f"devices={[(d['id'], d['name'], d['ready']) for d in c['devices']]}")
print({k: state[k] for k in ('exit_name', 'generation', 'autonomous', 'cancelled', 'remaining',
                             'deadline', 'bot_ready', 'delivery', 'snapshot_age')})
PY
}

cmd_password() {
  local count=${1:-1000}
  node "${EXITS[0]}" "PYTHONPATH=/opt/vpn2-emergency python3 - $count" <<'PY'
import string, sys
from vpn2_emergency import web
ok = 0
for _ in range(int(sys.argv[1])):
    p = web.generate_password()
    ok += (len(p) == 30 and set(p) <= set(string.ascii_letters + string.digits)
           and any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p))
print(f'{ok}/{sys.argv[1]} паролей: 30 знаков A-Z a-z 0-9, все группы')
PY
}

command=$1
shift
case $command in
  status) status ;;
  tunnels) normal_tunnels ;;
  quiet) cmd_quiet "$@" ;;
  start) cmd_start ;;
  deliveries) deliveries "$@" ;;
  hold) cmd_hold "${1:-60}" ;;
  restart-leader) cmd_restart_leader ;;
  kill-leader) cmd_kill_leader ;;
  split) cmd_split "$1" ;;
  restart-exits) cmd_restart_exits ;;
  telegram-block) cmd_telegram_block "${1:-5}" ;;
  sessions-blind) cmd_sessions_blind "${1:-60}" ;;
  entry-not-ready) cmd_entry_not_ready "${1:-5}" ;;
  recover) cmd_recover ;;
  recover-cancel) cmd_recover_cancel ;;
  finish) cmd_finish ;;
  password) cmd_password "$@" ;;
  client) cmd_client "$@" ;;
  indicator) cmd_indicator "${1:-}" ;;
  *) usage ;;
esac
exit "$FAILED"
