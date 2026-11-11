#!/usr/bin/env bash
# Статика страницы аварийки из образа. Реестр нужен только здесь; остановленный
# контейнер держит образ занятым и показывает версию в `docker ps -a`.
# Аргументы: каталог библиотек деплоя, каталог аварийки. Печатает image=<образ>
# и changed=1, если статика на узле сменилась.
set -euo pipefail
lib=${1:?каталог библиотек}
home=${2:?каталог аварийки}
venv=${VPN2_VENV:-/opt/vpn2/venv}
container=vpn2-emergency
# shellcheck source=infra/lib/release.sh
source "$lib/release.sh"
# Пакет выпуска на узле лежит в venv, копии рядом с библиотекой нет.
release_py() { "$venv/bin/python" -m vpn2.release "$@"; }

image=$(release_image emergency "${EMERGENCY_IMAGE:-}" "${EMERGENCY_IMAGE_PULL:-true}")
printf 'image=%s\n' "$image"
id=$(docker image inspect --format '{{.Id}}' "$image")
held=$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)
recorded=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["image"])' \
  "$home/release.json" 2>/dev/null || true)
if [[ "$held" == "$id" && "$recorded" == "$image" && -f "$home/static/index.html" ]]; then
  exit 0
fi

docker rm -f "$container" >/dev/null 2>&1 || true
docker create --name "$container" --network none "$image" >/dev/null
staging=$(mktemp -d "$home/.staging.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
docker cp "$container:/opt/vpn2-emergency/." "$staging/"
# Образ, собранный до переноса агента в venv, держит статику внутри пакета.
static=$staging/static
[[ -d "$static" ]] || static=$staging/vpn2/emergency/static
for required in index.html instructions.html; do
  [[ -f "$static/$required" ]] || {
    echo "ERROR: в образе аварийки $image нет статики страницы ($required)" >&2
    exit 1
  }
done
# Статика меняется целиком: файл, которого в новой версии нет, на узле не остаётся.
chmod -R u=rwX,go=rX "$static"
rm -rf -- "$home/static.old"
if [[ -d "$home/static" ]]; then mv "$home/static" "$home/static.old"; fi
mv "$static" "$home/static"
rm -rf -- "$home/static.old"
python3 - "$staging/image.json" "$image" > "$staging/release.json" <<'PY'
import json
import sys
release = json.load(open(sys.argv[1]))
print(json.dumps({'version': release['version'], 'revision': release['revision'], 'image': sys.argv[2]}))
PY
install -m 0644 "$staging/release.json" "$home/release.json"
# На узле остаётся ровно установленный образ. Прежний потерял тег при новом
# pull, а образы без тега видны только с --all.
while read -r old; do
  [[ "$old" == "$image" ]] || docker image rm -f "$old" >/dev/null
done < <(docker images --all --quiet --no-trunc --filter label=vpn2.component=emergency | sort -u)
echo changed=1
