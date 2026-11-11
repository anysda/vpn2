#!/usr/bin/env bash
# Пакеты раннера без зеркал Ubuntu во время прогона.
#   debs.sh install <пакет>...   поставить пакеты: набор .deb из ghcr, без сети apt;
#                                набора нет - зеркала с ограниченными повторами
#   debs.sh publish              собрать набор всех пакетов packages.txt под образ
#                                этого раннера и выложить в ghcr (images.yml)
#   debs.sh tag                  тег набора
# Набор - образ из одних .deb и индекса Packages: тег = образ раннера + список
# пакетов. Ставит apt из локального каталога, других источников у него нет.
set -euo pipefail

here=$(dirname "$(realpath "$0")")
list=$here/packages.txt
repo=${GITHUB_REPOSITORY:-anysda/vpn2}
ref_base=${APT_DEBS_REF:-ghcr.io/${repo,,}/panel}
work=${APT_DEBS_DIR:-/var/tmp/vpn2-apt-debs}
t0=$SECONDS

say() { printf '[apt-install] %s\n' "$*"; }
die() { printf '::error::apt-install: %s\n' "$*"; exit 1; }

all_packages() { grep -Ev '^[[:space:]]*(#|$)' "$list" | sort -u; }

# Образ раннера сменился или список пакетов - тег другой, набор собирается заново.
tag() {
  local id ver
  # shellcheck disable=SC1091
  id=$(. /etc/os-release; echo "$ID$VERSION_ID")
  ver=${ImageVersion:-local}
  echo "ci-debs-1-$id-$ver-$(dpkg --print-architecture)-$(all_packages | sha256sum | cut -c1-12)"
}

# Набор из ghcr: анонимно, пакет panel публичный.
pull() {
  local ref id err
  ref=$ref_base:$(tag)
  if ! err=$(timeout 90 docker pull -q "$ref" 2>&1 >/dev/null); then
    say "набора $ref нет: $(printf '%s' "$err" | tail -n1)"
    return 1
  fi
  sudo rm -rf "$work"; mkdir -p "$work"
  id=$(docker create "$ref" -)
  docker cp "$id:/debs/." "$work/" >/dev/null
  docker rm "$id" >/dev/null
  say "набор $ref: $(find "$work" -name '*.deb' | wc -l) .deb, $(du -sh "$work" | cut -f1), $((SECONDS - t0)) с"
}

# Запасные зеркала вне зоны ubuntu.com: сбой DNS Canonical их не задевает.
# Подписи архива те же - apt сверяет их ключом Ubuntu из образа раннера.
mirrors=${APT_DEBS_MIRRORS-https://mirrors.edge.kernel.org/ubuntu https://ftp.halifax.rwth-aachen.de/ubuntu}

# Параметры apt для попытки: пусто - штатные зеркала раннера, иначе одно запасное.
sources() {
  local mirror=$1 st code
  [ -n "$mirror" ] || return 0
  st=$(mktemp -d); mkdir -p "$st/lists/partial"; chmod 755 "$st"
  # shellcheck disable=SC1091
  code=$(. /etc/os-release; echo "$VERSION_CODENAME")
  printf 'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] %s %s main universe restricted multiverse\n' \
    "$mirror" "$code" "$mirror" "$code-updates" "$mirror" "$code-security" > "$st/sources.list"
  printf '%s\n' -o "Dir::Etc::sourcelist=$st/sources.list" -o Dir::Etc::sourceparts=- \
    -o "Dir::State::lists=$st/lists" -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache=
}

# С сети: штатные зеркала раннера, затем запасные. Попытка не дольше 90 с и с
# сообщением - не тишина до предела шага. Качаются только .deb, которых нет в
# образе раннера.
fetch() {
  local n=0 log mirror total src=() all=('')
  sudo rm -rf "$work"; mkdir -p "$work"
  log=$(mktemp)
  local net=(-o Acquire::Retries=0 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15
    -o DPkg::Lock::Timeout=60)
  read -ra src <<< "$mirrors"; all+=("${src[@]}"); total=${#all[@]}
  for mirror in "${all[@]}"; do
    n=$((n + 1))
    say "попытка $n из $total, ${mirror:-зеркала раннера}: индекс и .deb для $# пакетов"
    mapfile -t src < <(sources "$mirror")
    # shellcheck disable=SC2024  # журнал - свой файл, sudo для него не нужен
    if sudo timeout 30 apt-get -q --error-on=any "${net[@]}" "${src[@]}" update >"$log" 2>&1 \
      && sudo timeout 60 apt-get -q "${net[@]}" "${src[@]}" install -y --download-only --no-install-recommends \
        -o Dir::Cache::Archives="$work/archives" "$@" >>"$log" 2>&1; then
      if compgen -G "$work/archives/*.deb" >/dev/null; then sudo mv "$work"/archives/*.deb "$work/"; fi
      sudo rm -rf "$work/archives"; sudo chown -R "$(id -u):$(id -g)" "$work"
      say "с сети, ${mirror:-зеркала раннера}: $(find "$work" -name '*.deb' | wc -l) .deb, $(du -sh "$work" | cut -f1), $((SECONDS - t0)) с"
      return 0
    fi
    grep -E '^(E:|W:|Err:)' "$log" | head -n 5 | sed 's/^/[apt-install]   /' || :
    if grep -q '^E: Unable to locate package' "$log"; then
      die "пакет не найден в зеркалах: $(sed -n 's/^E: Unable to locate package //p' "$log" | tr '\n' ' ')"
    fi
    sudo rm -rf "$work/archives"
  done
  die "набора в ghcr нет, и ни штатные, ни запасные зеркала Ubuntu не ответили за $total попыток ($((SECONDS - t0)) с). Набор выкладывает images.yml на dev; зеркала оживут - перезапустить задание"
}

index() {
  local d
  for d in "$work"/*.deb; do
    [ -e "$d" ] || continue
    dpkg-deb -f "$d"
    printf 'Filename: ./%s\nSize: %s\nSHA256: %s\n\n' "${d##*/}" "$(stat -c %s "$d")" "$(sha256sum < "$d" | cut -d' ' -f1)"
  done > "$work/Packages"
}

# Единственный источник apt - каталог набора: к зеркалам запросов нет.
install_local() {
  local st p out
  st=$(mktemp -d); mkdir -p "$st/lists/partial"; chmod 755 "$st" "$work"
  printf 'deb [trusted=yes] file:%s ./\n' "$work" > "$st/sources.list"
  local loc=(-o Dir::Etc::sourcelist="$st/sources.list" -o Dir::Etc::sourceparts=- -o Dir::State::lists="$st/lists"
    -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= -o DPkg::Lock::Timeout=60)
  sudo apt-get -qq "${loc[@]}" update
  # Раннер одноразовый: dpkg без fsync, needrestart не опрашивает сервисы.
  if ! out=$(sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get -q "${loc[@]}" install -y \
    --no-install-recommends -o Dpkg::Options::=--force-unsafe-io "$@" 2>&1); then
    printf '%s\n' "$out" | tail -n 30
    die "из набора не встали: $* (см. вывод apt выше)"
  fi
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || die "пакет $p не установлен"
  done
}

cmd_install() {
  [ $# -ge 1 ] || die 'install: нужен хотя бы один пакет'
  local p mode=набор
  for p in "$@"; do
    all_packages | grep -qxF -- "$p" \
      || die "пакета $p нет в .github/actions/apt-install/packages.txt: без строки там он не попадёт в набор"
  done
  if ! pull; then
    mode='зеркала (набора нет)'
    fetch "$@"
    index
  fi
  install_local "$@"
  say "готово: $* - $mode, всего $((SECONDS - t0)) с"
}

cmd_publish() {
  local ref pkgs
  ref=$ref_base:$(tag)
  if docker manifest inspect "$ref" >/dev/null 2>&1; then
    say "набор $ref уже выложен"
    return 0
  fi
  mapfile -t pkgs < <(all_packages)
  fetch "${pkgs[@]}"
  index
  # В ghcr уходит только набор, из которого встали все пакеты списка.
  install_local "${pkgs[@]}"
  printf 'FROM scratch\nCOPY . /debs/\n' | docker build -q -t "$ref" -f - "$work" >/dev/null
  docker push -q "$ref" >/dev/null
  say "выложен $ref: $(find "$work" -name '*.deb' | wc -l) .deb, $(du -sh "$work" | cut -f1), $((SECONDS - t0)) с"
}

case ${1-} in
  install) shift; cmd_install "$@" ;;
  publish) cmd_publish ;;
  tag) tag ;;
  *) echo 'использование: debs.sh install <пакет>... | publish | tag' >&2; exit 2 ;;
esac
