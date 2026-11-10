#!/usr/bin/env bash
# venv с ansible-core и колесо vpn2 для деплоя. Файл для source; log и die
# приходят от вызывающего (infra/lib/ssh.sh), свои не определяются.
# Пути считаются от VPN2_ROOT и от каталога infra рядом с этим файлом.

VPN2_ROOT="${VPN2_ROOT:-/opt/vpn2}"
VENV_LOG="${VENV_LOG:-$VPN2_ROOT/ansible-venv.log}"
# Каталог infra с versions.yml, ansible/ и vpn2/; тест подменяет копией.
ANSIBLE_VENV_INFRA="${ANSIBLE_VENV_INFRA:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_VENV_PID=

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
    python3 -V 2>&1
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
  st="$(ls /usr/share/python-wheels/setuptools-*.whl 2>/dev/null | head -n1)"
  [[ -n "$st" ]] || die "ansible: нет setuptools-*.whl в /usr/share/python-wheels (пакет python3-venv)"
  tmp="$(mktemp -d)"
  cp -r "$ANSIBLE_VENV_INFRA/vpn2/." "$tmp/src"
  mkdir -p "$tmp/out"
  find "$tmp/src" -name __pycache__ -type d -prune -exec rm -rf {} +
  if ! python3 -I -c 'import sys, os; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2]); from setuptools import build_meta as b; b.build_wheel(sys.argv[3])' \
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

_ansible_venv_install_locked() {
  local venv="$VPN2_ROOT/venv" pins pip wheel src
  pins="$(ansible_venv_pins)"
  wheel="$(ansible_wheel_path)" || die "ansible: колеса vpn2 нет в $VPN2_ROOT/wheels"
  src="$(cat "$VPN2_ROOT/wheels/.src-sha256")"
  pip="$(ls /usr/share/python-wheels/pip-*.whl 2>/dev/null | head -n1)"
  if [[ -f "$venv/.vpn2-pins" && "$(cat "$venv/.vpn2-pins")" == "$pins" ]]; then
    if [[ -f "$venv/.vpn2-wheel" && "$(cat "$venv/.vpn2-wheel")" == "$src" ]]; then
      log "ansible: пины те же, без загрузки"
      return 0
    fi
    [[ -n "$pip" ]] || die "ansible: нет pip-*.whl в /usr/share/python-wheels (пакет python3-venv)"
    rm -f "$venv/.vpn2-wheel"
    python3 "$pip/pip" --python "$venv/bin/python" install --disable-pip-version-check \
      --no-index --no-deps --force-reinstall "$wheel" || return 1
    printf '%s\n' "$src" >"$venv/.vpn2-wheel"
    log "ansible: пины те же, колесо vpn2 обновлено"
    return 0
  fi
  [[ -n "$pip" ]] || die "ansible: нет pip-*.whl в /usr/share/python-wheels (пакет python3-venv)"
  rm -f "$venv/.vpn2-pins" "$venv/.vpn2-wheel"
  python3 -m venv --without-pip --clear "$venv" || return 1
  _venv_retry python3 "$pip/pip" --python "$venv/bin/python" install \
    --disable-pip-version-check -c "$ANSIBLE_VENV_INFRA/ansible/constraints.txt" ansible-core || return 1
  python3 "$pip/pip" --python "$venv/bin/python" install --disable-pip-version-check \
    --no-index --no-deps "$wheel" || return 1
  _venv_retry "$venv/bin/ansible-galaxy" collection install \
    -r "$ANSIBLE_VENV_INFRA/ansible/requirements.yml" -p "$VPN2_ROOT/collections" || return 1
  printf '%s\n' "$src" >"$venv/.vpn2-wheel"
  printf '%s\n' "$pins" >"$venv/.vpn2-pins"
}

ansible_venv_install() {
  mkdir -p "$VPN2_ROOT"
  (
    flock 9
    _ansible_venv_install_locked
  ) 9>"$VPN2_ROOT/.lock"
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

ansible_venv_wait() {
  [[ -n "$_VENV_PID" ]] || return 0
  local pid="$_VENV_PID"
  _VENV_PID=
  if ! wait "$pid"; then
    tail -n 20 "$VENV_LOG" >&2
    die "ansible: venv не установился, лог $VENV_LOG"
  fi
  cat "$VENV_LOG" >&2
}

# Стадия плеем ansible: infra/ansible/plays/<стадия>.yml на одном хосте.
# Стадии под fw-guard идут только bash-путём: сторож отката живёт там.
run_play() {
  local stage="$1" host="$2"
  [[ " $FW_GUARD_STAGES " != *" $stage "* ]] \
    || die "стадия $stage под fw-guard, плей пока нельзя (уберите ansible/plays/$stage.yml)"
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
  elif [[ ! -f "$VPN2_ROOT/venv/.vpn2-pins" && -z "$_VENV_PID" ]]; then
    ansible_venv_start
  fi
  ansible_venv_wait
  HOST_TAG="$host" log "[$stage → $host] starting"
  (
    set -o pipefail
    ANSIBLE_CONFIG="$DEPLOY_ROOT/ansible/ansible.cfg" VPN2_CONFIG="$DEPLOY_ROOT/../config.yaml" \
      "$VPN2_ROOT/venv/bin/ansible-playbook" "$DEPLOY_ROOT/ansible/plays/$stage.yml" \
      --limit "$host" --tags "$stage" 2>&1 | sed -u "s/^/[$host]/"
  )
}
