# anysda-vpn2 panel config — монтируется в контейнер как /etc/anysda/config.yaml
# Сгенерировано из infra/scripts/30-frontend.sh через envsubst.
#
# Только admin: остальные параметры панель читает из env-переменных (NUXT_*).
admin:
  user: ${ADMIN_USER}
  password: ${ADMIN_PASS}
