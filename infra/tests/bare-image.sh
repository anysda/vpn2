#!/bin/bash
# Первый запуск на образе без python3 и python3-yaml (шаблон LXC, debootstrap): ./deploy.sh ставит
# yaml сам. Идёт в системном контейнере (checks.yml), дерево - в /src.
#   bare-image.sh          ./deploy.sh -key ждёт занятый apt, ставит python3-yaml и печатает один ключ
#   bare-image.sh offline  без сети - отказ строкой deploy.sh, код 1, без Traceback
set -uo pipefail
fail() { echo "✗ $*"; exit 1; }
cp -a /src /opt/vpn2 && cd /opt/vpn2 || fail 'дерево не скопировано'
! command -v python3 >/dev/null || fail "в образе уже есть python3: проба ничего не проверяет"
if [[ ${1:-} == offline ]]; then
  out=$(./deploy.sh -key 2>&1); rc=$?
  echo "$out"
  [[ $rc == 1 ]] || fail "без сети -key: код $rc, ждём 1"
  grep -qF '✗ python3-yaml не установился, причина выше' <<<"$out" || fail 'без сети нет строки об отказе установки python3-yaml'
  ! grep -q Traceback <<<"$out" || fail 'без сети Traceback'
  echo '✓ без сети: отказ строкой deploy.sh'
  exit 0
fi
# apt занят чужой установкой: deploy.sh ждёт её, а не падает.
flock /var/lib/dpkg/lock-frontend sleep 45 &
sleep 1
start=$SECONDS
key=$(./deploy.sh -key) || fail "-key: код $?"
echo "-key за $((SECONDS - start)) с: $key"
[[ $key =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ && $(wc -l <<<"$key") == 1 ]] || fail 'stdout -key - не один ключ'
python3 -c 'import yaml' || fail 'python3-yaml не установлен'
DEPLOY_ROOT=/opt/vpn2/infra PYTHONPATH=infra/vpn2/src python3 - <<'PY' || fail 'точки входа деплоя тянут модули вне stdlib и yaml'
import importlib, sys
for m in ('vpn2.deploy', 'vpn2.deploy.bootstrap', 'vpn2.deploy.__main__', 'vpn2.deploy.run', 'vpn2.manager', 'vpn2.wizard',
          'vpn2.tls', 'vpn2.release', 'vpn2.containers.compose', 'vpn2.firewall', 'vpn2.backup.keys', 'vpn2.nodevars'):
    importlib.import_module(m)
# PyYAML с расширением C тянет рантайм Cython; sitecustomize - от python3 Ubuntu.
own = {'vpn2', 'yaml', '_yaml', '__main__', 'sitecustomize', 'cython_runtime'}
extra = sorted(n for n in {m.split('.')[0] for m in sys.modules} - set(sys.stdlib_module_names) - own
               if not n.startswith('_cython_'))
print('вне stdlib:', extra or 'нет')
sys.exit(bool(extra))
PY
[[ $(./deploy.sh -key) == "$key" ]] || fail 'второй -key дал другой ключ'
echo '✓ образ без python3-yaml: -key ставит его сам'
