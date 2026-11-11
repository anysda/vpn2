#!/usr/bin/env bash
# venv с ansible-core и колесо vpn2 для деплоя. Файл для source; log и die
# приходят от вызывающего (infra/lib/ssh.sh), свои не определяются.
# Пути считаются от VPN2_ROOT и от каталога infra рядом с этим файлом.

VPN2_ROOT="${VPN2_ROOT:-/opt/vpn2}"
VENV_LOG="${VENV_LOG:-$VPN2_ROOT/ansible-venv.log}"
# Каталог infra с versions.yml, ansible/ и vpn2/; тест подменяет копией.
ANSIBLE_VENV_INFRA="${ANSIBLE_VENV_INFRA:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Каталог системных колёс python (setuptools, pip); тест подменяет.
PYTHON_WHEELS_DIR="${PYTHON_WHEELS_DIR:-/usr/share/python-wheels}"
# Системный интерпретатор: колёса из PYTHON_WHEELS_DIR собраны под него, а python3
# из PATH может оказаться чужим (pyenv, setup-python). Тест подменяет.
VPN2_PYTHON="${VPN2_PYTHON:-/usr/bin/python3}"
_VENV_PID=
_VENV_READY=

# Хэш исходников пакета vpn2: pyproject.toml и src/**/*.py, __pycache__ не входит.
_vpn2_src_hash() {
  local pkg="$ANSIBLE_VENV_INFRA/vpn2"
  {
    sha256sum "$pkg/pyproject.toml"
    find "$pkg/src" -name '*.py' -not -path '*/__pycache__/*' -print0 \
      | LC_ALL=C sort -z | xargs -0 -r sha256sum
  } | sha256sum | cut -d' ' -f1
}

# Хэш пинов: версия python и два файла пинов; исходники пакета и versions.yml не входят.
ansible_venv_pins() {
  local f
  {
    "$VPN2_PYTHON" -V 2>&1
    for f in ansible/constraints.txt ansible/requirements.yml; do
      sha256sum "$ANSIBLE_VENV_INFRA/$f"
    done
  } | sha256sum | cut -d' ' -f1
}

# Путь колеса vpn2 в $VPN2_ROOT/wheels; пусто и код 1, если его нет.
ansible_wheel_path() {
  local w
  w="$(ls "$VPN2_ROOT"/wheels/vpn2-*-py3-none-any.whl 2>/dev/null | head -n1)"
  [[ -n "$w" ]] || return 1
  printf '%s\n' "$w"
}

# Колесо vpn2 без сети и без pip: PEP 517 хук setuptools из системного whl,
# в копии исходников, чтобы дерево репы осталось чистым. Под своим замком:
# сборку зовут параллельные дорожки экзитов.
ansible_wheel_build() {
  mkdir -p "$VPN2_ROOT/wheels"
  (
    flock 8
    _ansible_wheel_build_locked
  ) 8>"$VPN2_ROOT/wheels/.lock"
}

_ansible_wheel_build_locked() {
  local wheels="$VPN2_ROOT/wheels" src tmp st built
  src="$(_vpn2_src_hash)"
  if ansible_wheel_path >/dev/null && [[ -f "$wheels/.src-sha256" \
        && "$(cat "$wheels/.src-sha256")" == "$src" ]]; then
    return 0
  fi
  st="$(ls "$PYTHON_WHEELS_DIR"/setuptools-*.whl 2>/dev/null | head -n1 || true)"
  if [[ -z "$st" ]]; then
    # Entry, обновлённый со старого выпуска, без python3-venv: ставим его тем же
    # способом, что и install_prereqs; замок колеса держится, дорожки ждут.
    _install_prereq_pkgs python3-venv || true
    st="$(ls "$PYTHON_WHEELS_DIR"/setuptools-*.whl 2>/dev/null | head -n1 || true)"
  fi
  [[ -n "$st" ]] || die "ansible: нет setuptools-*.whl в $PYTHON_WHEELS_DIR (пакет python3-venv)"
  # setuptools до 70.1 (Ubuntu 24.04) не несёт bdist_wheel, он в пакете wheel;
  # на 26.04 первая проверка проходит и пакет не ставится.
  if ! "$VPN2_PYTHON" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import setuptools.command.bdist_wheel' "$st" 2>/dev/null \
      && ! "$VPN2_PYTHON" -I -c 'import wheel' 2>/dev/null; then
    _install_prereq_pkgs python3-wheel || true
  fi
  tmp="$(mktemp -d)"
  cp -r "$ANSIBLE_VENV_INFRA/vpn2/." "$tmp/src"
  mkdir -p "$tmp/out"
  find "$tmp/src" -name __pycache__ -type d -prune -exec rm -rf {} +
  if ! "$VPN2_PYTHON" -I -c 'import sys, os; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2]); from setuptools import build_meta as b; b.build_wheel(sys.argv[3])' \
      "$st" "$tmp/src" "$tmp/out" >"$tmp/build.log" 2>&1; then
    tail -n 20 "$tmp/build.log" >&2
    rm -rf "$tmp"
    die "ansible: колесо vpn2 не собралось"
  fi
  built="$(basename "$tmp"/out/vpn2-*.whl)"
  rm -f "$wheels"/vpn2-*.whl "$wheels/.src-sha256"
  mv -f "$tmp/out/$built" "$wheels/$built" \
    && printf '%s\n' "$src" >"$wheels/.src-sha256.new" \
    && mv -f "$wheels/.src-sha256.new" "$wheels/.src-sha256"
  rm -rf "$tmp"
}

# Команда с повтором: 3 попытки по 5 с.
_venv_retry() {
  local n
  for n in 1 2 3; do
    "$@" && return 0
    [[ $n -lt 3 ]] && sleep 5
  done
  return 1
}

# Сборка занята: её путь стоит в командной строке живого процесса. Служба,
# запущенная из venv, держит настоящий каталог сборки, а не симлинк.
_venv_in_use() {
  local f
  for f in /proc/[0-9]*/cmdline; do
    [[ "$(tr '\0' ' ' 2>/dev/null <"$f")" == *"/venvs/$1/"* ]] && return 0
  done
  return 1
}

# Сносит сборки, кроме действующей и занятых процессами; оборванная уходит тоже.
_venv_prune() {
  local keep dir
  keep="$(readlink "$VPN2_ROOT/venv" 2>/dev/null || true)"
  rm -rf "$VPN2_ROOT/venvs/.replaced"
  for dir in "$VPN2_ROOT"/venvs/*/; do
    [[ -d "$dir" ]] || continue
    dir="${dir%/}"
    [[ "venvs/${dir##*/}" != "$keep" ]] || continue
    _venv_in_use "${dir##*/}" || rm -rf "$dir"
  done
}

# Обмен именами одним системным вызовом: renameat2(RENAME_EXCHANGE). mv умеет
# его с coreutils 9.5, на Ubuntu 24.04 (9.4) тот же вызов делает python.
_venv_exchange() {
  mv --exchange -T "$1" "$2" 2>/dev/null && return 0
  "$VPN2_PYTHON" -I -c 'import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
a, b = (os.fsencode(x) for x in sys.argv[1:3])
sys.exit(0 if libc.renameat2(-100, a, -100, b, 2) == 0 else 1)' "$1" "$2" 2>/dev/null
}

# $VPN2_ROOT/venv - симлинк на сборку $1 из venvs: подмена одним переименованием,
# команда из venv запускается в любой миг.
_venv_switch() {
  local venv="$VPN2_ROOT/venv" aside="$VPN2_ROOT/venvs/.replaced"
  rm -rf "$venv.new"
  ln -s "venvs/$1" "$venv.new" || return 1
  if [[ -d "$venv" && ! -L "$venv" ]]; then
    # Каталог прежних выпусков меняется с симлинком именами; итог сверяется по
    # самому имени, а не по коду возврата.
    _venv_exchange "$venv.new" "$venv" || true
    if [[ -L "$venv" ]]; then
      rm -rf "$venv.new"
      return 0
    fi
    # Файловая система без обмена имён: один раз окно в два переименования.
    rm -rf "$aside"
    mv "$venv" "$aside" || return 1
    mv -T "$venv.new" "$venv" || return 1
    rm -rf "$aside"
    return 0
  fi
  mv -T "$venv.new" "$venv"
}

# Полная сборка $1: venv и ansible-core из склада колёс $VPN2_ROOT/pip-wheels
# (сеть нужна только при смене пинов), сверху колесо vpn2.
_venv_build_full() {
  local new="$1" pins="$2" pip="$3" wheel="$4" store="$VPN2_ROOT/pip-wheels"
  local constraints="$ANSIBLE_VENV_INFRA/ansible/constraints.txt"
  if [[ "$(cat "$store/.vpn2-pins" 2>/dev/null || true)" != "$pins" ]]; then
    rm -rf "$store"
    mkdir -p "$store"
    _venv_retry "$VPN2_PYTHON" "$pip/pip" download --disable-pip-version-check \
      --only-binary :all: -d "$store" -c "$constraints" ansible-core || return 1
    printf '%s\n' "$pins" >"$store/.vpn2-pins"
  fi
  "$VPN2_PYTHON" -m venv --without-pip "$new" || return 1
  "$VPN2_PYTHON" "$pip/pip" --python "$new/bin/python" install --disable-pip-version-check \
    --no-index --find-links "$store" -c "$constraints" ansible-core || return 1
  "$VPN2_PYTHON" "$pip/pip" --python "$new/bin/python" install --disable-pip-version-check \
    --no-index --no-deps "$wheel"
}

# Сменилось только колесо: сборка $1 - копия действующей $2 с переставленным
# пакетом vpn2, секунды вместо установки ansible-core. Копия настоящая, не
# жёсткие ссылки: действующая сборка не меняется ни в одном файле. Запускалки
# в bin несут полный путь сборки и переводятся на новую.
_venv_build_copy() {
  local new="$1" old="$2" pip="$3" wheel="$4"
  rm -rf "$new.part"
  cp -a "$old" "$new.part" || return 1
  rm -f "$new.part/.vpn2-pins" "$new.part/.vpn2-wheel"
  mv -T "$new.part" "$new" || return 1
  grep -rlF "$old/" "$new/bin" | xargs -r sed -i "s|$old/|$new/|g" || return 1
  "$VPN2_PYTHON" "$pip/pip" --python "$new/bin/python" install --disable-pip-version-check \
    --no-index --no-deps --force-reinstall "$wheel"
}

# Новая сборка ставится рядом, в $VPN2_ROOT/venvs/<пины>-<колесо>, действующая
# до подмены не трогается: оборванная установка оставляет каталог без метки
# .vpn2-pins, следующий запуск его сносит. Метка .vpn2-wheel - отпечаток всей
# сборки (исходники и пины): по ней роли перезапускают службы пакета, и смена
# одних зависимостей доходит до демонов.
_ansible_venv_install_locked() {
  local venv="$VPN2_ROOT/venv" pins pip wheel src had new old stamp
  pins="$(ansible_venv_pins)"
  wheel="$(ansible_wheel_path)" || die "ansible: колеса vpn2 нет в $VPN2_ROOT/wheels"
  src="$(cat "$VPN2_ROOT/wheels/.src-sha256")"
  pip="$(ls "$PYTHON_WHEELS_DIR"/pip-*.whl 2>/dev/null | head -n1)"
  had="$(cat "$venv/.vpn2-pins" 2>/dev/null || true)"
  stamp="$src $pins"
  if [[ "$had" == "$pins" && "$(cat "$venv/.vpn2-wheel" 2>/dev/null || true)" == "$stamp" ]]; then
    _venv_prune
    log "ansible: пины те же, без загрузки"
    return 0
  fi
  [[ -n "$pip" ]] || die "ansible: нет pip-*.whl в $PYTHON_WHEELS_DIR (пакет python3-venv)"
  new="$VPN2_ROOT/venvs/${pins:0:12}-${src:0:12}"
  if [[ ! -f "$new/.vpn2-pins" ]]; then
    rm -rf "$new" "$new.part"
    mkdir -p "$VPN2_ROOT/venvs"
    old="$(readlink -f "$venv" 2>/dev/null || true)"
    if [[ "$had" == "$pins" && -L "$venv" && "$old" == "$VPN2_ROOT/venvs/"* && "$old" != "$new" ]]; then
      _venv_build_copy "$new" "$old" "$pip" "$wheel" || return 1
    else
      _venv_build_full "$new" "$pins" "$pip" "$wheel" || return 1
    fi
  fi
  if [[ "$had" != "$pins" ]]; then
    _venv_retry "$new/bin/ansible-galaxy" collection install \
      -r "$ANSIBLE_VENV_INFRA/ansible/requirements.yml" -p "$VPN2_ROOT/collections" || return 1
  fi
  printf '%s\n' "$stamp" >"$new/.vpn2-wheel"
  printf '%s\n' "$pins" >"$new/.vpn2-pins"
  _venv_switch "${new##*/}" || return 1
  _venv_prune
  [[ "$had" != "$pins" ]] || log "ansible: пины те же, колесо vpn2 обновлено"
}

ansible_venv_install() {
  mkdir -p "$VPN2_ROOT"
  # stdin свой: ssh соседних цепочек делает общий stdin деплоя неблокирующим,
  # и ansible-galaxy на нём отказывает.
  (
    flock 9
    _ansible_venv_install_locked
  ) 9>"$VPN2_ROOT/.lock" </dev/null
}

# Закрывает унаследованные дескрипторы старше 2 (кроме 255 - файл самого
# скрипта): иначе фон держит замок деплоя верхнего deploy.sh, и после сбоя
# деплоя следующий запуск видит «deploy уже выполняется».
_close_inherited_fds() {
  local f n
  for f in /proc/$BASHPID/fd/*; do
    n="${f##*/}"
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    (( n > 2 && n != 255 )) || continue
    eval "exec $n>&-" 2>/dev/null || true
  done
}

# Колесо собирается синхронно, установка идёт в фоне.
ansible_venv_start() {
  ansible_wheel_build
  mkdir -p "$(dirname "$VENV_LOG")"
  ( _close_inherited_fds; ansible_venv_install ) >"$VENV_LOG" 2>&1 &
  _VENV_PID=$!
}

# Ожидание фоновой установки. Без аргумента сбой фатален (плею venv нужен);
# с `soft` - только предупреждение: хосты уже изменены, venv ни одной стадии не нужен.
ansible_venv_wait() {
  [[ -n "$_VENV_PID" ]] || return 0
  local pid="$_VENV_PID" mode="${1:-}"
  _VENV_PID=
  if ! wait "$pid"; then
    tail -n 20 "$VENV_LOG" >&2
    if [[ "$mode" == soft ]]; then
      HOST_TAG=deploy warn "ansible: venv не установился, лог $VENV_LOG; повторится при следующем запуске"
      return 0
    fi
    die "ansible: venv не установился, лог $VENV_LOG"
  fi
  cat "$VENV_LOG" >&2
}

# venv готов к этой строке: фон дожидается, без фона ставится сейчас.
# fresh - bash-стадии, зовущей команду vpn2: одиночный запуск после обновления
# репы сверяет колесо с исходниками (при тех же пинах ничего не качает).
ansible_venv_ensure() {
  if [[ "$BASHPID" != "$$" ]]; then
    # Хосты идут субшеллами: фон родителя не наш потомок, wait его не дождётся.
    # Своя установка встаёт за его замком и при тех же пинах ничего не качает.
    # Синхронно и без файла лога: успех молчит, неуспех показывает хвост.
    local install_log
    _VENV_PID=
    install_log="$(mktemp)"
    ansible_wheel_build
    if ! ansible_venv_install >"$install_log" 2>&1; then
      tail -n 20 "$install_log" >&2
      rm -f "$install_log"
      die "ansible: venv не установился"
    fi
    rm -f "$install_log"
  elif [[ -z "$_VENV_PID" ]] && { [[ ! -f "$VPN2_ROOT/venv/.vpn2-pins" ]] \
      || [[ "${1:-}" == fresh && -z "$_VENV_READY" ]]; }; then
    ansible_venv_start
  fi
  ansible_venv_wait
  _VENV_READY=1
}

# Стадия плеем ansible: плей infra/ansible/site.yml с тегом стадии на одном хосте.
# Обвес тот же, что у стадии-скрипта: окружение хоста, манифест выпуска,
# подготовка apt и сторож отката фаервола с пульсом.
run_play() {
  local stage="$1" host="$2"
  ansible_venv_ensure
  load_env "$host" || return 1
  log "[$stage → $host] starting"
  push "$DEPLOY_ROOT/lib/anysda-svc.sh" || return 1
  if [[ -n "${VPN2_RELEASE_MANIFEST:-}" && -f "$VPN2_RELEASE_MANIFEST" ]]; then
    push "$VPN2_RELEASE_MANIFEST" release-manifest.json || return 1
  fi
  local wheel='' wheel_sha=''
  if [[ "$host" != ru ]]; then
    ansible_wheel_build || return 1
    wheel="$(ansible_wheel_path)" || die "ansible: колеса vpn2 нет в $VPN2_ROOT/wheels"
    wheel_sha="$(sha256sum "$wheel" | cut -d' ' -f1)"
  fi
  # Пауза автообновлений и ожидание apt идут здесь, с живым выводом: внутри
  # задачи ansible двадцать минут ожидания были бы тишиной.
  local prepare=apt_prepare
  [[ "$stage" != 00-bootstrap ]] || prepare='apt_prepare; apt_auto_pause; apt_sshd_first'
  ssh_exec "rm -f /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg &&
bash --norc -euo pipefail -c 'HOST_TAG=$stage; source /tmp/anysda/anysda-svc.sh; $prepare'" | sed -u "s/^/[$host]/" || return 1
  local rc=0 beat='' owner=$BASHPID
  if fw_guard_stage "$stage"; then
    push "$DEPLOY_ROOT/lib/fw-guard.sh" || return 1
    ssh_exec "bash --norc /tmp/anysda/fw-guard.sh arm $stage $FW_GUARD_WINDOW" || return 1
    # shellcheck disable=SC2034  # читают fw_guard_beat и fw_guard_disarm в deploy.sh
    FW_GUARD_ENTRY_REFS=''
    [[ "$HOST_TAG" != ru ]] || fw_guard_entry_baseline "$stage"
    fw_guard_beat "$owner" &
    beat=$!
  fi
  (
    set -o pipefail
    export VPN2_DEVELOPMENT="${VPN2_DEVELOPMENT:-false}" VPN2_PANEL_FOLLOWS="${VPN2_PANEL_FOLLOWS:-false}"
    export VPN2_RELEASE_MANIFEST=/tmp/anysda/release-manifest.json
    export VPN2_WHEEL="$wheel" VPN2_WHEEL_SHA256="$wheel_sha" VPN2_INFRA="$DEPLOY_ROOT"
    VPN2_SOURCE_ROOT="$(cd "$DEPLOY_ROOT/.." && pwd)"
    export VPN2_SOURCE_ROOT
    # После restore стадии читают пароль панели из восстановленного файла,
    # а не засевают его заново из config.yaml.
    [[ "${RESTORE_PRESERVE_ADMIN:-0}" != 1 ]] || unset ADMIN_PASSWORD
    [[ "${RESTORE_PRESERVE_ADMIN:-0}" != 1 ]] || export VPN2_PRESERVE_ADMIN=1
    ANSIBLE_CONFIG="$DEPLOY_ROOT/ansible/ansible.cfg" VPN2_CONFIG="$DEPLOY_ROOT/../config.yaml" \
      "$VPN2_ROOT/venv/bin/ansible-playbook" "$DEPLOY_ROOT/ansible/site.yml" \
      --limit "$host" --tags "$stage" </dev/null 2>&1 | sed -u "s/^/[$host]/"
  ) || rc=$?
  if [[ -n "$beat" ]]; then
    kill "$beat" 2>/dev/null || true
    wait "$beat" 2>/dev/null || true
    fw_guard_disarm "$stage" || rc=1
  fi
  if [[ "$host" != ru ]]; then
    ssh_exec 'rm -rf /tmp/anysda' || warn "[$stage → $host] не удалось убрать /tmp/anysda"
  fi
  [[ $rc -eq 0 ]] || return "$rc"
  ok "[$stage → $host] done"
}
