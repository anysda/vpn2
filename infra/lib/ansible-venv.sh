#!/usr/bin/env bash
# venv с ansible-core и колесо vpn2 для деплоя. Файл для source; log и die - от вызывающего.
# $VPN2_ROOT/venv - симлинк на сборку venvs/<пины>-<исходники>: подмена одним переименованием,
# команда из venv запускается в любой миг. Сеть нужна только при смене пинов.

VPN2_ROOT="${VPN2_ROOT:-/opt/vpn2}"
ANSIBLE_VENV_INFRA="${ANSIBLE_VENV_INFRA:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_WHEELS_DIR="${PYTHON_WHEELS_DIR:-/usr/share/python-wheels}"
# Колёса из PYTHON_WHEELS_DIR собраны под системный python3, а в PATH может быть чужой.
VPN2_PYTHON="${VPN2_PYTHON:-/usr/bin/python3}"

_vpn2_src_hash() {
  local pkg="$ANSIBLE_VENV_INFRA/vpn2"
  {
    sha256sum "$pkg/pyproject.toml"
    find "$pkg/src" -name '*.py' -not -path '*/__pycache__/*' -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
  } | sha256sum | cut -d' ' -f1
}

ansible_venv_pins() {
  { "$VPN2_PYTHON" -V 2>&1; sha256sum "$ANSIBLE_VENV_INFRA"/ansible/{constraints.txt,requirements.yml}; } \
    | sha256sum | cut -d' ' -f1
}

ansible_wheel_path() {
  local w
  w="$(ls "$VPN2_ROOT"/wheels/vpn2-*-py3-none-any.whl 2>/dev/null | head -n1)"
  [[ -n "$w" ]] && printf '%s\n' "$w"
}

# Колесо без сети и без pip: PEP 517 хук setuptools из системного whl, в копии исходников.
_ansible_wheel_build_locked() {
  local wheels="$VPN2_ROOT/wheels" src st tmp
  src="$(_vpn2_src_hash)"
  if ansible_wheel_path >/dev/null && [[ "$(cat "$wheels/.src-sha256" 2>/dev/null)" == "$src" ]]; then
    return 0
  fi
  st="$(ls "$PYTHON_WHEELS_DIR"/setuptools-*.whl 2>/dev/null | head -n1)"
  [[ -n "$st" ]] || die "ansible: нет setuptools-*.whl в $PYTHON_WHEELS_DIR (пакет python3-venv)"
  tmp="$(mktemp -d)"
  cp -r "$ANSIBLE_VENV_INFRA/vpn2/." "$tmp/src"
  find "$tmp/src" -name __pycache__ -type d -prune -exec rm -rf {} +
  mkdir "$tmp/out"
  if ! "$VPN2_PYTHON" -I -c 'import sys, os; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2])
from setuptools import build_meta
build_meta.build_wheel(sys.argv[3])' "$st" "$tmp/src" "$tmp/out" >"$tmp/build.log" 2>&1; then
    tail -n 20 "$tmp/build.log" >&2
    rm -rf "$tmp"
    die "ansible: колесо vpn2 не собралось"
  fi
  rm -f "$wheels"/vpn2-*.whl "$wheels/.src-sha256"
  mv -f "$tmp"/out/vpn2-*.whl "$wheels/" && printf '%s\n' "$src" >"$wheels/.src-sha256"
  rm -rf "$tmp"
}

ansible_wheel_build() {
  mkdir -p "$VPN2_ROOT/wheels"
  ( flock 8; _ansible_wheel_build_locked ) 8>"$VPN2_ROOT/wheels/.lock"
}

_venv_retry() {
  local n
  for n in 1 2 3; do
    "$@" && return 0
    (( n == 3 )) || sleep 5
  done
  return 1
}

# Сносит сборки, кроме действующей и тех, чей путь в командной строке живого процесса.
_venv_prune() {
  local keep dir
  keep="$(readlink "$VPN2_ROOT/venv" 2>/dev/null)"
  for dir in "$VPN2_ROOT"/venvs/*/; do
    dir="${dir%/}"
    [[ -d "$dir" && "venvs/${dir##*/}" != "$keep" ]] || continue
    # [/] - чтобы grep не нашёл свою же командную строку.
    grep -qsE "/venvs/${dir##*/}[/]" /proc/[0-9]*/cmdline || rm -rf "$dir"
  done
}

_pip() { "$VPN2_PYTHON" "$PIP_WHL/pip" --disable-pip-version-check "$@"; }

# Полная сборка $1 из склада колёс $VPN2_ROOT/pip-wheels, склад обновляется при смене пинов $2.
_venv_build_full() {
  local new="$1" pins="$2" store="$VPN2_ROOT/pip-wheels" c="$ANSIBLE_VENV_INFRA/ansible/constraints.txt"
  if [[ "$(cat "$store/.vpn2-pins" 2>/dev/null)" != "$pins" ]]; then
    rm -rf "$store"
    mkdir -p "$store"
    _venv_retry _pip download --only-binary :all: -d "$store" -c "$c" ansible-core || return 1
    printf '%s\n' "$pins" >"$store/.vpn2-pins"
  fi
  "$VPN2_PYTHON" -m venv --without-pip "$new" || return 1
  _pip --python "$new/bin/python" install --no-index --find-links "$store" -c "$c" ansible-core || return 1
  _pip --python "$new/bin/python" install --no-index --no-deps "$WHEEL"
}

# Сменилось только колесо: копия действующей сборки $2 с переставленным пакетом vpn2.
# Запускалки в bin несут полный путь сборки и переводятся на новую.
_venv_build_copy() {
  local new="$1" old="$2"
  rm -rf "$new.part"
  cp -a "$old" "$new.part" || return 1
  rm -f "$new.part/.vpn2-pins" "$new.part/.vpn2-wheel"
  mv -T "$new.part" "$new" || return 1
  grep -rlF "$old/" "$new/bin" | xargs -r sed -i "s|$old/|$new/|g" || return 1
  _pip --python "$new/bin/python" install --no-index --no-deps --force-reinstall "$WHEEL"
}

# Новая сборка ставится рядом, действующая до подмены не трогается; оборванная остаётся
# без метки .vpn2-pins и пересобирается. .vpn2-wheel - отпечаток сборки, по нему роли
# перезапускают службы пакета.
_ansible_venv_install_locked() {
  local venv="$VPN2_ROOT/venv" pins had new old stamp
  pins="$(ansible_venv_pins)"
  WHEEL="$(ansible_wheel_path)" || die "ansible: колеса vpn2 нет в $VPN2_ROOT/wheels"
  stamp="$(cat "$VPN2_ROOT/wheels/.src-sha256") $pins"
  had="$(cat "$venv/.vpn2-pins" 2>/dev/null)"
  if [[ "$had" == "$pins" && "$(cat "$venv/.vpn2-wheel" 2>/dev/null)" == "$stamp" ]]; then
    _venv_prune
    log "ansible: пины те же, без загрузки"
    return 0
  fi
  PIP_WHL="$(ls "$PYTHON_WHEELS_DIR"/pip-*.whl 2>/dev/null | head -n1)"
  [[ -n "$PIP_WHL" ]] || die "ansible: нет pip-*.whl в $PYTHON_WHEELS_DIR (пакет python3-venv)"
  new="$VPN2_ROOT/venvs/${pins:0:12}-${stamp:0:12}"
  if [[ ! -f "$new/.vpn2-pins" ]]; then
    rm -rf "$new" "$new.part"
    mkdir -p "$VPN2_ROOT/venvs"
    old="$(readlink -f "$venv" 2>/dev/null)"
    if [[ "$had" == "$pins" && -n "$old" && "$old" != "$new" ]]; then
      _venv_build_copy "$new" "$old" || return 1
    else
      _venv_build_full "$new" "$pins" || return 1
    fi
  fi
  if [[ "$had" != "$pins" ]]; then
    _venv_retry "$new/bin/ansible-galaxy" collection install \
      -r "$ANSIBLE_VENV_INFRA/ansible/requirements.yml" -p "$VPN2_ROOT/collections" || return 1
  fi
  printf '%s\n' "$stamp" >"$new/.vpn2-wheel"
  printf '%s\n' "$pins" >"$new/.vpn2-pins"
  ln -sfn "venvs/${new##*/}" "$venv.new" && mv -T "$venv.new" "$venv" || return 1
  _venv_prune
  [[ "$had" != "$pins" ]] || log "ansible: пины те же, колесо vpn2 обновлено"
}

# Колесо и venv под текущие пины: при тех же пинах - меньше секунды. stdin свой:
# ssh соседних цепочек делает общий stdin неблокирующим, и ansible-galaxy на нём отказывает.
ansible_venv_ensure() {
  local log=$VPN2_ROOT/ansible-venv.log
  mkdir -p "$VPN2_ROOT"
  { ansible_wheel_build && ( flock 9; _ansible_venv_install_locked ) 9>"$VPN2_ROOT/.lock"; } \
    </dev/null >"$log" 2>&1 || { tail -n 20 "$log" >&2; die "ansible: venv не установился, лог $log"; }
}
