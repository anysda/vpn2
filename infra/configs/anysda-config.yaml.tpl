# anysda-vpn2 panel config — монтируется в контейнер как /etc/anysda/config.yaml
# Сгенерировано из infra/scripts/30-frontend.sh через envsubst.
#
# Только admin: панель читает SS-port/cipher/public-host из env-переменных
# (NUXT_SS_*), не из этого файла.
admin:
  user: ${ADMIN_USER}
  password: ${ADMIN_PASS}
