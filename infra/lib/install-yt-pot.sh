#!/usr/bin/env bash
# Вызывается стадией 19 после установки выделенного Node/npm. GPL-3.0-only,
# исходники и LICENSE остаются рядом с собранным кодом; демона и порта нет.
set -euo pipefail
probe_venv=${1:?путь venv видеопробы}
pot_version=2.0.2
if [[ "$("$probe_venv/bin/python3" -c 'from importlib.metadata import version; print(version("bgutil-ytdlp-pot-provider"))' 2>/dev/null || true)" != "$pot_version" ]]; then
  "$probe_venv/bin/pip" install --disable-pip-version-check "bgutil-ytdlp-pot-provider==$pot_version"
fi
if [[ -s "$probe_venv/bgutil/server/build/generate_once.js" && -f "$probe_venv/bgutil/.installed" ]] \
   && [[ "$(<"$probe_venv/bgutil/.installed")" == "$pot_version" ]]; then
  exit 0
fi
pot_tmp=$(mktemp -d "$probe_venv/.pot-install.XXXXXXXX")
trap 'rm -rf "$pot_tmp"' EXIT
curl -fsSL --retry 3 --connect-timeout 20 --max-time 180 \
  "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/$pot_version.tar.gz" -o "$pot_tmp/source.tar.gz"
printf '%s  %s\n' 1f2ae9ec39405b8e80397dfc2551f58deed09d8dff4b2b8fbc314df8e091c0a1 \
  "$pot_tmp/source.tar.gz" | sha256sum -c -
tar -xzf "$pot_tmp/source.tar.gz" -C "$pot_tmp"
export PATH="$probe_venv/bin:$PATH"
export npm_config_cache="$pot_tmp/npm-cache"
pot_source="$pot_tmp/bgutil-ytdlp-pot-provider-$pot_version"
(
  cd "$pot_source/server"
  "$probe_venv/bin/node" "$probe_venv/lib/node_modules/npm/bin/npm-cli.js" ci --no-audit --no-fund
  "$probe_venv/bin/node" node_modules/typescript/bin/tsc
  "$probe_venv/bin/node" "$probe_venv/lib/node_modules/npm/bin/npm-cli.js" prune --omit=dev --no-audit --no-fund
)
# Метка только после успешной сборки. Ошибка не оставляет «готовую» установку.
printf '%s\n' "$pot_version" > "$pot_source/.installed"
if [[ -e "$probe_venv/bgutil" ]]; then
  mv "$probe_venv/bgutil" "$pot_tmp/previous"
fi
mv "$pot_source" "$probe_venv/bgutil"
