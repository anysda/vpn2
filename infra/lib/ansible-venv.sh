#!/usr/bin/env bash
# venv с ansible-core и колесо vpn2 для деплоя. Файл для source; log и die
# приходят от вызывающего (infra/lib/ssh.sh), свои не определяются.
# Пути считаются от VPN2_ROOT и от каталога infra рядом с этим файлом.

VPN2_ROOT="${VPN2_ROOT:-/opt/vpn2}"
VENV_LOG="${VENV_LOG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/state/ansible-venv.log}"
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

# Хэш пинов: версия python, три файла пинов и исходники пакета.
ansible_venv_pins() {
  local f
  {
    python3 -V 2>&1
    for f in versions.yml ansible/constraints.txt ansible/requirements.yml; do
      sha256sum "$ANSIBLE_VENV_INFRA/$f"
    done
    _vpn2_src_hash
  } | sha256sum | cut -d' ' -f1
}

# Колесо vpn2 без сети и без pip: PEP 517 хук setuptools из системного whl,
# в копии исходников, чтобы дерево репы осталось чистым.
ansible_wheel_build() {
  local wheels="$VPN2_ROOT/wheels" src tmp st
  src="$(_vpn2_src_hash)"
  if [[ -f "$wheels/vpn2.whl" && -f "$wheels/.src-sha256" \
        && "$(cat "$wheels/.src-sha256")" == "$src" ]]; then
    return 0
  fi
  st="$(ls /usr/share/python-wheels/setuptools-*.whl 2>/dev/null | head -n1)"
  [[ -n "$st" ]] || die "ansible: нет setuptools-*.whl в /usr/share/python-wheels (пакет python3-venv)"
  mkdir -p "$wheels"
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
  basename "$tmp"/out/vpn2-*.whl >"$wheels/.name"
  mv -f "$tmp"/out/vpn2-*.whl "$wheels/vpn2.whl.new" \
    && mv -f "$wheels/vpn2.whl.new" "$wheels/vpn2.whl" \
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
  local venv="$VPN2_ROOT/venv" pins pip link
  pins="$(ansible_venv_pins)"
  if [[ -f "$venv/.vpn2-pins" && -f "$VPN2_ROOT/wheels/vpn2.whl" \
        && "$(cat "$venv/.vpn2-pins")" == "$pins" ]]; then
    log "ansible: пины те же, без загрузки"
    return 0
  fi
  pip="$(ls /usr/share/python-wheels/pip-*.whl 2>/dev/null | head -n1)"
  [[ -n "$pip" ]] || die "ansible: нет pip-*.whl в /usr/share/python-wheels (пакет python3-venv)"
  rm -f "$venv/.vpn2-pins"
  python3 -m venv --without-pip --clear "$venv" || return 1
  _venv_retry python3 "$pip/pip" --python "$venv/bin/python" install \
    --disable-pip-version-check -c "$ANSIBLE_VENV_INFRA/ansible/constraints.txt" ansible-core || return 1
  # pip требует настоящее имя колеса: ссылка с ним на файл с фиксированным именем.
  link="$(mktemp -d)"
  ln -s "$VPN2_ROOT/wheels/vpn2.whl" "$link/$(cat "$VPN2_ROOT/wheels/.name")"
  python3 "$pip/pip" --python "$venv/bin/python" install --disable-pip-version-check \
    --no-index --no-deps "$link"/*.whl || { rm -rf "$link"; return 1; }
  rm -rf "$link"
  _venv_retry "$venv/bin/ansible-galaxy" collection install \
    -r "$ANSIBLE_VENV_INFRA/ansible/requirements.yml" -p "$VPN2_ROOT/collections" || return 1
  printf '%s\n' "$pins" >"$venv/.vpn2-pins"
}

ansible_venv_install() {
  mkdir -p "$VPN2_ROOT"
  (
    flock 9
    _ansible_venv_install_locked
  ) 9>"$VPN2_ROOT/.lock"
}

# Колесо собирается синхронно, установка идёт в фоне.
ansible_venv_start() {
  ansible_wheel_build
  mkdir -p "$(dirname "$VENV_LOG")"
  ansible_venv_install >"$VENV_LOG" 2>&1 &
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
