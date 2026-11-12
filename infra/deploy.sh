#!/usr/bin/env bash
# anysda-vpn2 - оркестратор деплоя. Стадии - плеи infra/ansible/site.yml (тег = стадия),
# служебные шаги вокруг них - infra/ansible/deploy.yml. Здесь только порядок, журнал,
# сторож фаервола и то, что видит оператор. Запускается на entry через ./deploy.sh.
set -euo pipefail

# Юнит systemd без User= и systemd-run переменную HOME не задают, а ключ оркестратора живёт в ней.
if [[ -z "${HOME:-}" ]]; then
  HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
  [[ -n "$HOME" ]] || { echo 'deploy: у пользователя нет домашнего каталога (HOME пуст, в passwd его нет)' >&2; exit 1; }
fi
DEPLOY_ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO=$(dirname "$DEPLOY_ROOT")
export HOME DEPLOY_ROOT

if [[ -t 2 ]]; then C_R='\033[31m' C_G='\033[32m' C_Y='\033[33m' C_B='\033[34m' C_END='\033[0m'
else C_R='' C_G='' C_Y='' C_B='' C_END=''; fi
HOST_TAG=deploy
log()  { printf '%b[%s]%b %s\n' "$C_B" "${HOST_TAG:-?}" "$C_END" "$*" >&2; }
ok()   { printf '%b[%s]%b %s\n' "$C_G" "${HOST_TAG:-?}" "$C_END" "$*" >&2; }
warn() { printf '%b[%s]%b %s\n' "$C_Y" "${HOST_TAG:-?}" "$C_END" "$*" >&2; }
die()  { printf '%b[%s] FATAL:%b %s\n' "$C_R" "${HOST_TAG:-?}" "$C_END" "$*" >&2; exit 1; }
step() { printf '%b==>%b %s\n' "$C_B" "$C_END" "$*"; }

for lib in state-dir anysda-svc ansible-venv deploy-report deploy-prep deploy-backup; do
  # shellcheck disable=SC1090
  source "$DEPLOY_ROOT/lib/$lib.sh"
done

_STAGE_LOG=() _VERIFY_WARNINGS=() _DEPLOY_WARNINGS=() SKIPPED_EXIT_TAGS='' _TOTAL_T0=0
MUTATING=false OPENED=false TMP='' BEAT='' PLAY=''
MANIFEST=${VPN2_RELEASE_MANIFEST:-}
GUARDED=' 00-bootstrap 05-mgmt-mesh 10-foreign 19-yt-zapret 20-ru-router 22-adguard 23-emergency 27-ikev2 28-wireguard 29-openvpn 30-frontend '
GUARD_WINDOW=${VPN2_FW_GUARD_WINDOW:-90}
FRESH_LOGIN='-o ControlMaster=no -o ControlPath=none'
export VPN2_DEVELOPMENT=${VPN2_DEVELOPMENT:-false} VPN2_INFRA=$DEPLOY_ROOT VPN2_SOURCE_ROOT=$REPO
export VPN2_RELEASE_MANIFEST=/tmp/anysda/release-manifest.json

inventory() {
  PYTHONPATH="$DEPLOY_ROOT/vpn2/src" VPN2_CONFIG="$REPO/config.yaml" python3 -B "$DEPLOY_ROOT/ansible/inventory.py" "$@"
}

# Переменные узлов из config.yaml на время запуска: ENVS/<узел>.env для source.
envs_write() {
  local node
  ENVS=$TMP/envs
  install -d -m 700 "$ENVS"
  inventory --env ru > "$ENVS/ru.env" || exit 1
  for node in $(exit_tags); do inventory --env "$node" > "$ENVS/$node.env" || exit 1; done
}

exit_tags() { ( source "$ENVS/ru.env"; echo "${EXIT_TAGS:-}" ); }

# Группа → узлы через пробел; экзиты, пропущенные в pre-flight, в стадии не идут.
expand_hosts() {
  local group=$1 tag all active=()
  all=$(exit_tags)
  for tag in $all; do grep -qw -- "$tag" <<<"$SKIPPED_EXIT_TAGS" || active+=("$tag"); done
  case "$group" in
    ru) echo ru ;;
    foreign|all)
      [[ -n "$all" ]] || die "EXIT_TAGS пустой — перезапусти ./deploy.sh"
      [[ "$group" == foreign ]] && echo "${active[*]}" || echo "ru ${active[*]}" ;;
    *) grep -qw -- "$group" <<<"${active[*]}" \
         || die "unknown host group: $group (доступные: ru foreign all или тег из config.yaml)"
       echo "$group" ;;
  esac
}

# Узлы стадии через запятую: порядок и принадлежность стадий знает site.yml.
stage_nodes() {
  local out rc=0
  out=$(PYTHONPATH="$DEPLOY_ROOT/vpn2/src" python3 -B -m vpn2.stages nodes \
          --site "$DEPLOY_ROOT/ansible/site.yml" "$@" 2>&1) || rc=$?
  (( rc == 0 )) || die "$out"
  tr ' ' ',' <<<"$out"
}

# Плей идёт фоном под wait: сигнал оператора прерывает ожидание сразу, а не после стадии.
play() {
  local rc=0
  ANSIBLE_CONFIG="$DEPLOY_ROOT/ansible/ansible.cfg" VPN2_CONFIG="$REPO/config.yaml" \
    "$VPN2_ROOT/venv/bin/ansible-playbook" "$@" </dev/null &
  PLAY=$!
  wait "$PLAY" || rc=$?
  PLAY=''
  return "$rc"
}
service() { play "$DEPLOY_ROOT/ansible/deploy.yml" -e "manifest=$MANIFEST" "$@"; }

# Сторож фаервола (lib/fw-guard.sh на узле) откатывает ufw и таблицы стадии, если новые
# ssh-входы пропали дольше окна. Пульс - отдельный запуск с новым входом: открытое
# соединение стадии живёт и при сломанном правиле. Вход на entry снаружи видят экзиты.
guard_beat() {
  local nodes outside=false
  nodes=$(expand_hosts all | tr ' ' ',')
  ANSIBLE_SSH_ARGS=$FRESH_LOGIN service --tags beat --limit "$nodes" -e "out=$TMP" >/dev/null 2>&1 || true
  if [[ -e "$TMP/entry-outside" ]]; then
    outside=true
  elif [[ -z "${GUARD_WARNED:-}" ]]; then
    GUARD_WARNED=1
    warn '[fw-guard] вход на entry снаружи не проверяется: до стадии его не видит ни один экзит'
  fi
  while sleep 5; do
    ANSIBLE_SSH_ARGS=$FRESH_LOGIN service --tags beat --limit "$nodes" -e "entry_outside=$outside" >/dev/null 2>&1 || true
  done &
  BEAT=$!
}

guard_finish() {
  local stage=$1 nodes=$2 rc deadline=$(( $(date +%s) + GUARD_WINDOW + 30 ))
  kill "$BEAT" 2>/dev/null || true
  wait "$BEAT" 2>/dev/null || true
  BEAT=''
  # Код 4 - узел не пустил: ждём окно сторожа, он вернёт фаервол, и вход появится.
  while rc=0; ANSIBLE_SSH_ARGS=$FRESH_LOGIN service --tags finish --limit "$nodes" -e "stage=$stage" || rc=$?
        (( rc == 4 && $(date +%s) < deadline )); do sleep 5; done
  (( rc != 4 )) || warn "после стадии $stage нода не пускает новые ssh-входы дольше окна сторожа - проверь вход руками"
  return "$rc"
}

# Одна стадия на узлах (через запятую): подготовка, сторож, плей стадии, снятие сторожа.
stage_once() {
  local stage=$1 nodes=$2 rc=0
  log "[$stage → $nodes] starting"
  [[ "$GUARDED" != *" $stage "* ]] || guard_beat
  play "$DEPLOY_ROOT/ansible/deploy.yml" "$DEPLOY_ROOT/ansible/site.yml" --tags "prepare,$stage" \
    --limit "$nodes" -e "stage=$stage" -e "manifest=$MANIFEST" || rc=$?
  if [[ -n "$BEAT" ]]; then
    guard_finish "$stage" "$nodes" || rc=1
  else
    service --tags cleanup --limit "$nodes" >/dev/null 2>&1 || warn "[$stage → $nodes] не удалось убрать /tmp/anysda"
  fi
  return "$rc"
}

# 99-verify: 0 - всё измерено, 2 - не всё (установка годна), 1 - отказ.
verify() {
  local nodes=$1 rc=0 result=0 host code text found
  VPN2_VERIFY_OUT=$(mktemp -d -p "$TMP")
  export VPN2_VERIFY_OUT
  stage_once 99-verify "$nodes" || rc=$?
  for host in ${nodes//,/ }; do
    code='' text=''
    [[ ! -f "$VPN2_VERIFY_OUT/$host" ]] || { IFS= read -r code; text=$(cat); } < "$VPN2_VERIFY_OUT/$host"
    [[ -z "$text" ]] || printf '%s\n' "$text"
    found=$(grep -E '^\[.*\] ⚠ WARP: ' <<<"$text" || true)
    [[ -z "$found" ]] || mapfile -t -O "${#_DEPLOY_WARNINGS[@]}" _DEPLOY_WARNINGS <<<"$found"
    if [[ $rc -eq 0 && "$code" == 2 ]]; then
      warn "[99-verify → $host] не все проверки измерены"
      found=$(grep -E '^\[.*\] \? .*не проверено:' <<<"$text" || true)
      mapfile -t -O "${#_VERIFY_WARNINGS[@]}" _VERIFY_WARNINGS \
        <<<"${found:-[$host] ? не проверено: 99-verify вернул код 2; подробности выше}"
      (( result == 1 )) || result=2
    elif [[ $rc -ne 0 || "$code" != 0 ]]; then
      result=1
    else
      ok "[99-verify → $host] done"
    fi
  done
  return "$result"
}

# После 20-ru-router vpn2 ports check меряет hy2-direct экзитов и подбирает порт. Перекатку
# стадий он просит в fd 3 строкой `rerun журнал стадия:группа...` и ждёт её код на stdin.
verify_and_rotate_ports() {
  local screen requests pid kind rest pair rc answers=$TMP/ports-answers
  step 'verify: проверяю tunnels via clash-api + UDP-пробник для мертвецов'
  rm -f "$answers"
  mkfifo "$answers"
  exec {screen}>&1
  # shellcheck disable=SC2046  # экзиты через пробел
  exec {requests}< <(PYTHONPATH="$DEPLOY_ROOT/vpn2/src" VPN2_CONFIG="$REPO/config.yaml" \
    python3 -B -m vpn2.cli ports check $(expand_hosts foreign) 3>&1 >&"$screen" <>"$answers")
  pid=$!
  while IFS=' ' read -r -u "$requests" kind rest; do
    [[ "$kind" == rerun ]] || { _STAGE_LOG+=("$rest"); continue; }
    envs_write
    rc=0
    # shellcheck disable=SC2086  # стадии через пробел
    ( for pair in ${rest#* }; do run_stage "${pair%%:*}" "${pair##*:}"; done ) >"${rest%% *}" 2>&1 || rc=$?
    echo "$rc" >"$answers"
  done
  exec {requests}<&- {screen}>&-
  wait "$pid" || exit 1
}

run_stage() {
  local stage=$1 group=$2 t0 hosts nodes host rc=0 t
  t0=$(date +%s)
  hosts=$(expand_hosts "$group")
  # shellcheck disable=SC2086  # список узлов через пробел
  nodes=$(stage_nodes "$stage" $hosts)
  [[ -n "$nodes" ]] || return 0
  case "$stage" in
    00-bootstrap) prep_keys ;;
    20-ru-router) pull_secrets; warn_insecure_exits ;;
    23-emergency) PYTHONPATH="$DEPLOY_ROOT/vpn2/src" python3 -B -m vpn2.emergency provision "$REPO" ;;
  esac
  if [[ "$stage" == 10-foreign ]]; then
    [[ $(exit_tags | wc -w) != 1 ]] || warn 'единственный экзит: на время его обновления возможен перерыв VPN'
    # Экзиты обновляются по одному, следующий - только когда предыдущий прошёл проверку.
    for host in ${nodes//,/ }; do
      stage_once "$stage" "$host" && verify "$host" || { rc=1; break; }
    done
  elif [[ "$stage" == 99-verify ]]; then
    for host in $SKIPPED_EXIT_TAGS; do
      printf '[%s] ⊘ пропущен: SSH недоступен в pre-flight\n' "$host"
      rc=2
    done
    verify "$nodes" || { (( $? == 2 )) && rc=2 || rc=1; }
    warn_insecure_exits
  else
    stage_once "$stage" "$nodes" && ok "[$stage → $nodes] done" || rc=1
  fi
  (( rc != 1 )) || die "стадия $stage: одна или несколько нод завершились с ошибкой"
  t=$(( $(date +%s) - t0 )); t="$(( t / 60 ))m $(( t % 60 ))s"
  _STAGE_LOG+=("$stage"$'\t'"$group"$'\t'"${t#0m }")
  [[ "$stage" != 99-verify || -z "${_VERIFY_WARNINGS[*]:-}" ]] \
    || warn '99-verify: ⚠ не все проверки измерены; установка не требует отката'
  return "$rc"
}

do_all() {
  local stage where group verify_rc=0
  print_banner
  _TOTAL_T0=$(date +%s)
  if [[ -d /run/systemd/system ]]; then
    nftables_preserve_rules
    step 'автообновления entry на паузу до конца деплоя'
    HOST_TAG=ru apt_auto_pause
  fi
  preflight_ssh
  while read -r stage where <&3; do
    case "$where" in entry) group=ru ;; exits) group=foreign ;; *) group=all ;; esac
    case "$stage" in
      99-verify)   run_stage "$stage" "$group" || verify_rc=$? ;;
      35-telegram) VPN2_PANEL_FOLLOWS=true run_stage "$stage" "$group" ;;
      *)           run_stage "$stage" "$group" ;;
    esac
    [[ "$stage" != 20-ru-router ]] || verify_and_rotate_ports
  done 3< <(PYTHONPATH="$DEPLOY_ROOT/vpn2/src" python3 -B -m vpn2.stages list --site "$DEPLOY_ROOT/ansible/site.yml")
  (( verify_rc != 1 )) || return 1
  play "$DEPLOY_ROOT/ansible/site.yml" --tags old-layout -e verify_old_layout=true --limit "$(expand_hosts all | tr ' ' ',')" >/dev/null \
    || warn 'остатки раскладки v1.1.5 не убраны: повтори ./deploy.sh'
  release_complete
  print_summary
  announce_deploy_done
  return "$verify_rc"
}

release_cli() { PYTHONPATH="$DEPLOY_ROOT/vpn2/src" python3 -m vpn2.release --manifest "$MANIFEST" "$@" "$REPO"; }

# Выпуск открывается до изменений: манифест сверен с деревом, журнал прошлого деплоя закрыт,
# на каждом узле проверки и снимок. С фазы applying узлы считаются изменёнными.
release_start() {
  local action=${1:-} out partial=false
  if [[ "$VPN2_DEVELOPMENT" == true || "$action" == rollback ]]; then
    MANIFEST=''
  else
    # Без --manifest берётся опубликованный манифест commit этого дерева.
    [[ -n "$MANIFEST" ]] || { MANIFEST=$VPN2_STATE_DIR/release-manifest.json; release_cli fetch || exit 1; }
    release_cli verify-tree || exit 1
    [[ "$MANIFEST" -ef "$VPN2_STATE_DIR/release-manifest.json" ]] \
      || install -m 600 "$MANIFEST" "$VPN2_STATE_DIR/release-manifest.json"
  fi
  tar -C "$DEPLOY_ROOT/vpn2/src" --exclude=__pycache__ -czf "$TMP/vpn2.tgz" vpn2
  if [[ "$action" == rollback ]]; then
    service --tags artifacts --limit ru -e "bundle=$TMP/vpn2.tgz" >/dev/null
    return 0
  fi
  if [[ "$action" != recover ]] && ! out=$(service --tags journal 2>&1); then
    out=$(grep -oE 'unfinished deploy on [^"]*\(or \./deploy\.sh rollback\)' <<<"$out" | head -n1 || true)
    die "${out:-журнал деплоя на узлах не прочитан: проверь вход по ssh}"
  fi
  [[ -z "$action" || "$action" == recover ]] || partial=true
  OPENED=true
  service --tags release -e "bundle=$TMP/vpn2.tgz" -e "partial=$partial" || exit 1
  MUTATING=true
}

release_complete() {
  [[ "$MUTATING" == true ]] || return 0
  service --tags complete -e "bundle=$TMP/vpn2.tgz" >/dev/null || die 'запись о выпуске не закрыта: повтори ./deploy.sh recover'
  MUTATING=false
}

on_exit() {
  local code=$?
  trap - EXIT INT TERM
  kill $BEAT $PLAY 2>/dev/null || true
  if (( code )) && [[ "$MUTATING" == true ]]; then
    echo 'deploy interrupted: operation journal retained; inspect services, then run ./deploy.sh recover (or ./deploy.sh rollback)' >&2
  elif (( code )) && [[ "$OPENED" == true ]]; then
    service --tags abort >/dev/null 2>&1 || true
  fi
  [[ -z "$TMP" ]] || rm -rf "$TMP"
  exit "$code"
}

case "${1:-}" in -h|--help) usage ;; esac
TMP=$(mktemp -d)
trap on_exit EXIT
envs_write
trap 'exit 130' INT
trap 'exit 143' TERM
dry_restore=false
[[ "${1:-}" != restore || " ${*:2} " != *' --dry-run '* ]] || dry_restore=true
# Ошибка в аргументах выпуск не открывает: иначе журнал остался бы в applying до recover.
case "${1:-}" in
  ''|recover|rollback) ;;
  backup)      do_backup; exit ;;
  backup-list) /usr/local/bin/anysda-backup-list.sh; exit ;;
  restore)     "$dry_restore" || restore_preflight "${@:2}" ;;
  *)
    [[ $# -ge 2 ]] || die "использование: ./deploy.sh <stage> <group>  (пример: ./deploy.sh 30-frontend ru)"
    stage_nodes "$1" >/dev/null
    group=$(expand_hosts "$2")
    # Стадия entry на экзите (и наоборот) - отказ, а не пустой проход с кодом 0.
    # shellcheck disable=SC2086  # список узлов через пробел
    stage_nodes --refuse-empty "$1" $group >/dev/null ;;
esac
"$dry_restore" || install_prereqs
[[ "${1:-}" == 99-verify ]] || "$dry_restore" || release_start "$@"
case "${1:-}" in
  ''|recover) do_all ;;
  restore)    do_restore "${@:2}" ;;
  rollback)   /opt/vpn2/venv/bin/vpn2 containers rollback && PYTHONPATH=/tmp/anysda python3 -m vpn2.cli state phase --phase rolled-back ;;
  *)          run_stage "$1" "$2"
              [[ "$1" != 30-frontend || "$2" == [!ra]* ]] || print_sso_notice ;;
esac
release_complete
