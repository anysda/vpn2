#!/usr/bin/env bash
# anysda-vpn — интерактивный конфигуратор
# Задаёт вопросы → записывает config.yaml → генерирует infra/envs/*.env

set -euo pipefail

# Принудительно UTF-8, чтобы пароли/тексты не уехали в cp1251 и не сломали YAML
export LANG=C.UTF-8 LC_ALL=C.UTF-8

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Цвета ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  B='\033[34m' G='\033[32m' Y='\033[33m' R='\033[31m' D='\033[2m' E='\033[0m'
else
  B= G= Y= R= D= E=
fi

header() { printf '\n%b  %s%b\n' "$B" "$*" "$E" >&2; }
hint()   { printf '%b  %s%b\n'   "$D" "$*" "$E" >&2; }
ok()     { printf '%b  ✓ %s%b\n' "$G" "$*" "$E" >&2; }
warn()   { printf '%b  ! %s%b\n' "$Y" "$*" "$E" >&2; }
fail()   { printf '%b  ✗ %s%b\n' "$R" "$*" "$E" >&2; }

ask() {
  local prompt="$1" default="${2:-}" answer
  if [[ -n "$default" ]]; then
    printf '  %b%s%b [%b%s%b]: ' "$Y" "$prompt" "$E" "$D" "$default" "$E" >&2
  else
    printf '  %b%s%b: ' "$Y" "$prompt" "$E" >&2
  fi
  read -r answer
  printf '%s' "${answer:-$default}"
}

ask_ip() {
  local prompt="$1" val
  while true; do
    val=$(ask "$prompt")
    if [[ "$val" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
      printf '%s' "$val"; return
    fi
    fail "Некорректный IP-адрес: $val"
  done
}

ask_int() {
  local prompt="$1" default="$2" val
  while true; do
    val=$(ask "$prompt" "$default")
    if [[ "$val" =~ ^[1-9][0-9]*$ ]]; then printf '%s' "$val"; return; fi
    fail "Введите целое число ≥ 1"
  done
}

ask_yn() {
  local prompt="$1" default="${2:-n}" val
  while true; do
    val=$(ask "$prompt (y/n)" "$default")
    case "${val,,}" in y|yes) printf 'y'; return;; n|no) printf 'n'; return;; esac
    fail "Введите y или n"
  done
}

sanitize() {
  printf '%s' "$1" | tr -d '\000-\010\013\014\016-\037\177' | sed 's/[[:space:]]*$//'
}

ask_password() {
  local prompt="$1" p1 p2
  while true; do
    printf '  %b%s%b: ' "$Y" "$prompt" "$E" >&2
    read -rs p1; printf '\n' >&2
    p1=$(sanitize "$p1")
    [[ -z "$p1" ]] && { fail "Пароль не может быть пустым"; continue; }
    printf '  %b%s (повтор)%b: ' "$Y" "$prompt" "$E" >&2
    read -rs p2; printf '\n' >&2
    p2=$(sanitize "$p2")
    if [[ "$p1" == "$p2" ]]; then printf '%s' "$p1"; return; fi
    fail "Пароли не совпадают"
  done
}

ask_password_once() {
  local prompt="$1" p1
  while true; do
    printf '  %b%s%b: ' "$Y" "$prompt" "$E" >&2
    read -rs p1; printf '\n' >&2
    p1=$(sanitize "$p1")
    [[ -z "$p1" ]] && { fail "Пароль не может быть пустым"; continue; }
    printf '%s' "$p1"; return
  done
}

detect_geo() {
  local ip="$1" result
  result=$(curl -sSf --max-time 5 "http://ip-api.com/json/${ip}?fields=countryCode,country" 2>/dev/null) \
    || { printf ':'; return; }
  printf '%s' "$result" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('countryCode','').lower() + ':' + d.get('country',''), end='')
except Exception:
    print(':', end='')
" 2>/dev/null || printf ':'
}

detect_entry_ip() {
  local ip=""
  local iface
  iface=$(ip -4 route show default 2>/dev/null | awk '/default/ {print $5; exit}')
  if [[ -n "$iface" ]]; then
    ip=$(ip -4 addr show dev "$iface" 2>/dev/null \
         | awk '/inet / {print $2; exit}' | cut -d/ -f1)
  fi
  if [[ -z "$ip" ]] || [[ "$ip" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|127\.) ]]; then
    local ext
    ext=$(curl -sSf --max-time 5 https://api.ipify.org 2>/dev/null) \
      || ext=$(curl -sSf --max-time 5 https://ifconfig.me 2>/dev/null) \
      || ext=""
    [[ -n "$ext" ]] && ip="$ext"
  fi
  printf '%s' "$ip"
}

suggest_tag() {
  local cc="$1" used="$2" tag n=2
  tag="$cc"
  while echo " $used " | grep -qw " $tag "; do
    tag="${cc}${n}"; (( n++ ))
  done
  printf '%s' "$tag"
}

validate_tag() {
  local tag="$1" used="$2"
  if ! [[ "$tag" =~ ^[a-z][a-z0-9-]*$ ]]; then
    fail "Тег: только строчные буквы, цифры, дефис (пример: us, se2)"; return 1
  fi
  if [[ "$tag" == "ru" ]]; then
    fail "Тег 'ru' зарезервирован для входной ноды"; return 1
  fi
  if echo " $used " | grep -qw " $tag "; then
    fail "Тег '$tag' уже используется"; return 1
  fi
  return 0
}

printf '
' >&2
printf '%b  ┌────────────────────────────────────────────┐%b
' "$B" "$E" >&2
printf '%b  │%b  anysda-vpn  ·  настройка инфраструктуры   %b│%b
' "$B" "$E" "$B" "$E" >&2
printf '%b  └────────────────────────────────────────────┘%b
' "$B" "$E" >&2
hint "Работает по IP — домены не нужны"
hint "Деплой идёт от root по SSH-паролю (config.yaml будет chmod 600)"
hint "Для редактирования запусти ./setup.sh повторно"

header "Сколько выходных серверов?"
hint "Минимум 1. Каждый даёт отдельный Hysteria2 endpoint."
n_exits=$(ask_int "Количество" "1")

header "Входная нода (entry) — WireGuard endpoint + панель управления"
entry_ip=$(detect_entry_ip)
if [[ -z "$entry_ip" ]]; then
  fail "Не удалось автоопределить IP. Введите вручную."
  entry_ip=$(ask_ip "IP-адрес входной ноды")
else
  ok "IP входной ноды: $entry_ip"
fi
entry_pass=$(ask_password_once "root password")

exits_yaml=""
used_tags=""
declare -A exit_ips=()
declare -A exit_pass=()

for i in $(seq 1 "$n_exits"); do
  header "Выходная нода $i / $n_exits"
  ex_ip=$(ask_ip "IP-адрес")
  ex_pass=$(ask_password_once "root password")

  printf '  %bОпределяю страну...%b ' "$D" "$E" >&2
  geo=$(detect_geo "$ex_ip")
  cc="${geo%%:*}"
  country="${geo#*:}"

  if [[ -n "$cc" ]]; then
    suggested=$(suggest_tag "$cc" "$used_tags")
    printf '%b✓%b %s (%s) → тег «%s»\n' "$G" "$E" "$country" "${cc^^}" "$suggested" >&2
    while true; do
      ex_tag=$(ask "Тег ноды" "$suggested")
      validate_tag "$ex_tag" "$used_tags" && break
    done
  else
    printf '%b?%b не определена\n' "$Y" "$E" >&2
    while true; do
      ex_tag=$(ask "Тег ноды (us / de / se / ...)")
      validate_tag "$ex_tag" "$used_tags" && break
    done
  fi

  used_tags="$used_tags $ex_tag"
  exit_ips["$ex_tag"]="$ex_ip"
  exit_pass["$ex_tag"]="$ex_pass"

  exits_yaml+="  - tag: ${ex_tag}\n    host: ${ex_ip}\n    password: ${ex_pass}\n\n"
done

header "Учётка администратора (веб-панель + AdGuard UI)"
admin_user=$(ask "Логин" "admin")
admin_user=$(sanitize "$admin_user")
hint "Пароль: Enter — сгенерировать случайный 18-символьный, иначе свой"
printf '  %b%s%b: ' "$Y" "Пароль (Enter — сгенерировать)" "$E" >&2
read -rs admin_pass; printf '\n' >&2
admin_pass=$(sanitize "$admin_pass")
if [[ -n "$admin_pass" ]]; then
  printf '  %b%s%b: ' "$Y" "Пароль (повтор)" "$E" >&2
  read -rs admin_pass2; printf '\n' >&2
  admin_pass2=$(sanitize "$admin_pass2")
  if [[ "$admin_pass" != "$admin_pass2" ]]; then
    fail "Пароли не совпадают — пароль будет сгенерирован автоматически"
    admin_pass=""
  fi
fi
[[ -z "$admin_pass" ]] && ok "Будет сгенерирован случайный пароль на ноде"

header "Домен для веб-панели (опционально)"
hint "Если задан — Caddy получит Let's Encrypt сертификат на http://<домен>/"
hint "A-запись должна указывать на IP входной ноды: $entry_ip"
hint "Hysteria2-туннели остаются на self-signed TLS (IP), домен — только для UI"
panel_domain=""
domain_enable=$(ask_yn "Настроить HTTPS-домен для панели?" "n")
if [[ "$domain_enable" == "y" ]]; then
  while true; do
    panel_domain=$(ask "Домен (например vpn.example.com)")
    panel_domain=$(sanitize "$panel_domain")
    if [[ "$panel_domain" =~ ^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
      printf '  %bпроверяю DNS...%b ' "$D" "$E" >&2
      resolved=$(dig +short +time=3 +tries=1 "$panel_domain" @8.8.8.8 2>/dev/null | tail -1)
      if [[ "$resolved" == "$entry_ip" ]]; then
        printf '%b✓%b A-запись → %s\n' "$G" "$E" "$resolved" >&2
        break
      elif [[ -n "$resolved" ]]; then
        warn "A-запись указывает на $resolved, а entry IP = $entry_ip"
        confirm_dns=$(ask_yn "Продолжить?" "n")
        [[ "$confirm_dns" == "y" ]] && break
      else
        warn "DNS не разрешает домен (либо A-записи нет, либо нужно подождать)"
        confirm_dns=$(ask_yn "Продолжить всё равно?" "n")
        [[ "$confirm_dns" == "y" ]] && break
      fi
    else
      fail "Неправильный формат домена"
    fi
  done
  ok "Домен: $panel_domain"
fi

header "Telegram-бот (опционально)"
hint "Уведомления: новый WG клиент, exit-нода упала (RTT=0 > 3 мин)"
hint "Нужно: создать бота через @BotFather, узнать свой Chat ID через @userinfobot"
tg_token=""
tg_chat_id=""
tg_admin_username=""
tg_enable=$(ask_yn "Включить Telegram-бот?" "n")
if [[ "$tg_enable" == "y" ]]; then
  while true; do
    tg_token=$(ask "Токен бота (@BotFather)")
    tg_token=$(sanitize "$tg_token")
    [[ -n "$tg_token" ]] && break
    fail "Токен не может быть пустым"
  done
  while true; do
    tg_chat_id=$(ask "Chat ID (число из @userinfobot)")
    tg_chat_id=$(sanitize "$tg_chat_id")
    if [[ "$tg_chat_id" =~ ^-?[0-9]+$ ]]; then break; fi
    fail "Chat ID — целое число (может быть отрицательным для группы)"
  done
  hint "Никнейм админа бота — его бот укажет людям без доступа («напишите @...»)"
  tg_admin_username=$(ask "Никнейм админа бота (@username, опционально)")
  tg_admin_username=$(sanitize "$tg_admin_username")
  tg_admin_username="${tg_admin_username#@}"
  ok "Telegram-бот настроен"
else
  hint "Пропущено. Можно добавить позже через setup.sh"
fi

# ── Backup секция ───────────────────────────────────────────────────────────
# Опциональная, но ВКЛЮЧЕНА по умолчанию — local backend без внешних зависимостей.
# Прометы строго по паттерну telegram/panel-domain: Enter-через-всё → enabled,
# local, off, auto-gen passphrase. S3-вопросы появляются только при выборе s3.
header "Бэкапы БД (опционально)"
hint "Архив: db.sqlite + /etc/anysda/* + WG server key + OpenVPN CA, шифруется"
hint "age. Backend local — без внешних зависимостей; S3 — любой S3-совместимый."
backup_enabled='y'
backup_backend='local'
backup_passphrase=''
backup_schedule='off'
backup_s3_endpoint=''
backup_s3_bucket=''
backup_s3_region=''
backup_s3_access_key=''
backup_s3_secret_key=''
backup_enabled_q=$(ask_yn "Включить бэкапы?" "y")
if [[ "$backup_enabled_q" == "y" ]]; then
  # Дефолт — daily в 02:00 по локальной TZ entry-ноды. Выключить можно явным "n".
  backup_schedule_q=$(ask_yn "Ежедневный бэкап в 02:00 (по локальной TZ entry, systemd-timer)?" "y")
  [[ "$backup_schedule_q" == "y" ]] && backup_schedule='daily'

  while true; do
    backup_backend=$(ask "Backend (local/s3)" "local")
    backup_backend=$(sanitize "$backup_backend")
    case "$backup_backend" in local|s3) break;; esac
    fail "только local или s3"
  done

  hint "Passphrase для шифрования архивов. Enter — сгенерировать."
  hint "⚠ Лишишься passphrase — лишишься возможности восстановить бэкапы."
  printf '  %b%s%b: ' "$Y" "Backup passphrase (Enter — auto-gen)" "$E" >&2
  read -rs backup_passphrase; printf '\n' >&2
  backup_passphrase=$(sanitize "$backup_passphrase")
  if [[ -z "$backup_passphrase" ]]; then
    backup_passphrase=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
    warn "Сгенерирован passphrase: $backup_passphrase"
    warn "Сохраните где-то ВНЕ entry-ноды. Дубль лежит в config.yaml (chmod 600)."
  fi

  if [[ "$backup_backend" == "s3" ]]; then
    hint "Endpoint любого S3-совместимого: MinIO/B2/R2/Yandex/Cloud.ru/AWS"
    while true; do
      backup_s3_endpoint=$(ask "S3 endpoint URL")
      backup_s3_endpoint=$(sanitize "$backup_s3_endpoint")
      [[ "$backup_s3_endpoint" =~ ^https?:// ]] && break
      fail "Endpoint должен начинаться с http:// или https://"
    done
    while true; do
      backup_s3_bucket=$(ask "S3 bucket")
      backup_s3_bucket=$(sanitize "$backup_s3_bucket")
      [[ -n "$backup_s3_bucket" ]] && break
    done
    hint "Region: cloud.ru→ru-central-1, yandex→ru-central1, selectel→ru-1, AWS→из endpoint"
    backup_s3_region=$(ask "S3 region" "us-east-1")
    backup_s3_region=$(sanitize "$backup_s3_region")
    backup_s3_access_key=$(ask "S3 access key ID")
    backup_s3_access_key=$(sanitize "$backup_s3_access_key")
    backup_s3_secret_key=$(ask_password_once "S3 secret access key")
  fi
  ok "Бэкапы настроены (backend=$backup_backend, schedule=$backup_schedule)"
else
  hint "Пропущено. Можно включить позже через setup.sh"
fi

# ── YouTube мимо экзитов ────────────────────────────────────────────────────
# YouTube не крутит рекламу на российских IP, поэтому гнать его через
# зарубежный экзит — значит включить её себе руками. Штатный режим: выпускать
# YouTube прямо с entry (РФ-адрес), а DPI провайдера обходить десинком
# (nfqws2, стадия 19-yt-zapret). Стадия сама проверяет A/B и валит деплой,
# если обход не заработал, — без неё YouTube лёг бы у всех клиентов.
header "YouTube (опционально)"
hint "Выпускать YouTube с entry-ноды (РФ-IP → без рекламы) + обход DPI десинком."
hint "Иначе YouTube поедет на экзит вместе со всем остальным — с рекламой."
youtube_route='off'
yt_q=$(ask_yn "Вывести YouTube на РФ-выход entry с обходом DPI?" "y")
if [[ "$yt_q" == "y" ]]; then
  youtube_route='zapret'
  ok "YouTube: РФ-выход + десинк (стадия 19-yt-zapret соберёт nfqws2 на entry)"
else
  hint "Пропущено. YouTube пойдёт через экзиты (с рекламой)."
fi

printf '\n' >&2
for _tag in "${!exit_ips[@]}"; do
  export "_EXIT_IP_${_tag}=${exit_ips[$_tag]}"
done
python3 - "$(printf %b "$B")" "$(printf %b "$E")" "$entry_ip" "${used_tags# }" <<'PYINNER'
import sys, os, unicodedata
B, E, entry_ip, tags_str = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
W = 44
def vis(t):
    return sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in t)
def pad(t):
    return t + ' ' * max(0, W - vis(t))
def row(t):
    return f"{B}  \u2502{E}{pad(t)}{B}\u2502{E}"
print(f"{B}  \u250c{'\u2500'*W}\u2510{E}")
print(row("  Конфигурация"))
print(f"{B}  \u251c{'\u2500'*W}\u2524{E}")
print(row(f"  Входная нода:  {entry_ip}"))
tags = tags_str.split() if tags_str.strip() else []
for tag in tags:
    ip = os.environ.get(f"_EXIT_IP_{tag}", "")
    print(row(f"  Выход {tag}: {ip}"))
print(f"{B}  \u2514{'\u2500'*W}\u2518{E}")
PYINNER

printf '\n' >&2
confirm=$(ask_yn "Сохранить config.yaml и сгенерировать envs?" "y")
[[ "$confirm" != "y" ]] && { printf '  Отменено.\n' >&2; exit 0; }

config_file="${REPO_ROOT}/config.yaml"

# Если config.yaml уже есть — сохраняем orchestrator-ключ: при перезапуске
# setup.sh он не должен потеряться, иначе повторный деплой сломает доступ.
orch_key=''; orch_pubkey=''
if [[ -f "$config_file" ]]; then
  orch_key=$(sed -n 's/^orchestrator_key:[[:space:]]*//p' "$config_file" | head -1)
  orch_pubkey=$(sed -n 's/^orchestrator_pubkey:[[:space:]]*//p' "$config_file" | head -1)
fi

ENTRY_IP="$entry_ip" \
ENTRY_PASS="$entry_pass" \
EXITS_YAML="$(printf '%b' "$exits_yaml")" \
ADMIN_USER="$admin_user" \
ADMIN_PASS="$admin_pass" \
PANEL_DOMAIN="$panel_domain" \
TG_TOKEN="$tg_token" \
TG_CHAT_ID="$tg_chat_id" \
TG_ADMIN_USERNAME="$tg_admin_username" \
BACKUP_ENABLED="$backup_enabled_q" \
BACKUP_BACKEND="$backup_backend" \
BACKUP_PASSPHRASE="$backup_passphrase" \
BACKUP_SCHEDULE="$backup_schedule" \
BACKUP_S3_ENDPOINT="$backup_s3_endpoint" \
BACKUP_S3_BUCKET="$backup_s3_bucket" \
BACKUP_S3_REGION="$backup_s3_region" \
BACKUP_S3_ACCESS_KEY="$backup_s3_access_key" \
BACKUP_S3_SECRET_KEY="$backup_s3_secret_key" \
YOUTUBE_ROUTE="$youtube_route" \
ORCH_KEY="$orch_key" \
ORCH_PUBKEY="$orch_pubkey" \
CONFIG_FILE="$config_file" \
python3 <<'PYEOF'
import os, datetime

lines = []
lines.append('# anysda-vpn — конфигурация (chmod 600 — содержит пароли)')
lines.append('# Создано ./setup.sh ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
lines.append('')
lines.append('entry:')
lines.append('  host: ' + os.environ['ENTRY_IP'])
lines.append('  password: ' + os.environ['ENTRY_PASS'])
lines.append('')
lines.append('exits:')
exits_block = os.environ.get('EXITS_YAML', '')
lines.append(exits_block.rstrip())
lines.append('')
lines.append('admin:')
lines.append('  user: ' + os.environ['ADMIN_USER'])
ap = os.environ.get('ADMIN_PASS', '')
if ap:
    lines.append('  password: ' + ap)
else:
    lines.append('  # password не задан — будет сгенерирован при первом деплое')

pd = os.environ.get('PANEL_DOMAIN', '')
if pd:
    lines.append('')
    lines.append('panel:')
    lines.append('  domain: ' + pd)

tt = os.environ.get('TG_TOKEN', '')
tc = os.environ.get('TG_CHAT_ID', '')
ta = os.environ.get('TG_ADMIN_USERNAME', '')
if tt and tc:
    lines.append('')
    lines.append('telegram:')
    lines.append('  bot_token: ' + tt)
    lines.append('  chat_id: ' + tc)
    if ta:
        lines.append('  admin_username: ' + ta)

# Backup-секция: если включена — записываем. Если выключена — секция не
# пишется вообще, чтобы 26-backup увидел отсутствие блока и пропустился.
be = os.environ.get('BACKUP_ENABLED', '')
if be == 'y':
    bb     = os.environ.get('BACKUP_BACKEND', 'local')
    bpwd   = os.environ.get('BACKUP_PASSPHRASE', '')
    bsched = os.environ.get('BACKUP_SCHEDULE', 'off')
    lines.append('')
    lines.append('backup:')
    lines.append('  enabled: true')
    lines.append('  backend: ' + bb)
    lines.append('  passphrase: ' + bpwd)
    lines.append('  retention: 10')
    lines.append('  schedule: ' + bsched)
    if bb == 's3':
        lines.append('  s3:')
        lines.append('    endpoint: '   + os.environ.get('BACKUP_S3_ENDPOINT', ''))
        lines.append('    bucket: '     + os.environ.get('BACKUP_S3_BUCKET',   ''))
        lines.append('    region: '     + os.environ.get('BACKUP_S3_REGION', 'us-east-1'))
        lines.append('    access_key: ' + os.environ.get('BACKUP_S3_ACCESS_KEY', ''))
        lines.append('    secret_key: ' + os.environ.get('BACKUP_S3_SECRET_KEY', ''))

# YouTube-секция: пишем только когда фича включена — отсутствие блока
# читается как youtube.route=off, то есть поведение до появления фичи.
yr = os.environ.get('YOUTUBE_ROUTE', 'off')
if yr and yr != 'off':
    lines.append('')
    lines.append('youtube:')
    lines.append('  route: ' + yr)
    lines.append('  quic: block')

orch_key = os.environ.get('ORCH_KEY', '')
orch_pubkey = os.environ.get('ORCH_PUBKEY', '')
if orch_key:
    lines.append('')
    lines.append('# orchestrator SSH-ключ — НЕ удалять: нужен для повторного деплоя.')
    lines.append('orchestrator_key: ' + orch_key)
    if orch_pubkey:
        lines.append('orchestrator_pubkey: ' + orch_pubkey)

with open(os.environ['CONFIG_FILE'], 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')
PYEOF

chmod 600 "$config_file"

printf '\n' >&2
python3 "${REPO_ROOT}/infra/lib/config2env.py" "$REPO_ROOT"

printf '\n' >&2
ok "Готово!"
printf '\n' >&2
printf '\n' >&2
printf '  Следующий шаг:  %b./deploy.sh%b\n\n' "$G" "$E" >&2