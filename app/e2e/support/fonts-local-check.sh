#!/usr/bin/env bash
# Шрифт панели в образе - из локального пакета @fontsource-variable/manrope, а не
# из Google Fonts: файлы /_fonts/*.woff2 образа совпадают по sha256 с файлами пакета,
# ссылок на fonts.googleapis.com и fonts.gstatic.com в сборке нет.
#
#   bash e2e/support/fonts-local-check.sh vpn2-panel:e2e-vpn
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source-path=SCRIPTDIR source=panel-stand.sh
source "$APP_DIR/e2e/support/panel-stand.sh"
IMAGE=${1:?использование: fonts-local-check.sh <образ панели>}

fail() { echo "✘ $*" >&2; exit 1; }

pkg_json=$(cd "$APP_DIR" && node -p "require.resolve('@fontsource-variable/manrope/package.json')") \
  || fail "пакет @fontsource-variable/manrope не найден от $APP_DIR (npm ci не выполнен?)"
pkg_files=$(dirname "$pkg_json")/files

pkg_list=$(find "$pkg_files" -maxdepth 1 -name '*.woff2' -exec sha256sum {} + | awk '{print $1}') \
  || fail "не прочитан каталог $pkg_files"
[ -n "$pkg_list" ] || fail "в пакете нет файлов woff2: $pkg_files"

image_list=$(dk run --rm --network none --entrypoint sh "$IMAGE" -c \
  'set -e; d=/app/.output/public/_fonts; [ -d "$d" ] || exit 3; cd "$d"; for f in *.woff2; do [ -e "$f" ] || continue; echo "$(sha256sum "$f" | cut -d" " -f1)  $(wc -c < "$f")  $f"; done') \
  || fail "не удалось прочитать /app/.output/public/_fonts из образа $IMAGE (нет каталога или сбой docker run)"

count=0
bad=0
while read -r sha size name; do
  [ -n "$sha" ] || continue
  count=$((count + 1))
  if ! grep -qx "$sha" <<< "$pkg_list"; then
    echo "✘ $name ($size байт, sha256 $sha) не совпал ни с одним файлом пакета" >&2
    bad=1
  fi
done <<< "$image_list"

[ "$count" -ge 2 ] || fail "в образе woff2 шрифтов: $count, ожидалось не меньше двух"
[ "$bad" -eq 0 ] || fail "шрифты образа не из пакета @fontsource-variable/manrope"

refs=$(dk run --rm --network none --entrypoint sh "$IMAGE" -c \
  'grep -rlE "fonts\.googleapis\.com|fonts\.gstatic\.com" /app/.output/public /app/.output/server | head -5 || true') \
  || fail "сбой docker run при поиске ссылок на Google Fonts"
if [ -n "$refs" ]; then
  echo "$refs" >&2
  fail "в сборке есть ссылки на fonts.googleapis.com или fonts.gstatic.com"
fi

echo "шрифты образа из пакета: $count файлов"
