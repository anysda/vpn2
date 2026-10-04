# anysda-vpn2 panel config — монтируется в контейнер как /etc/anysda/config.yaml
# Сгенерировано из infra/scripts/30-frontend.sh через envsubst.
#
# Только admin: остальные параметры панель читает из env-переменных (NUXT_*).
# *_YAML — значения уже в двойных кавычках (JSON-строка = YAML-строка), иначе
# пароль `12345678` читался числом, `!Qwerty1` тегом, а `x #y` обрезался.
admin:
  user: ${ADMIN_USER_YAML}
  password: ${ADMIN_PASS_YAML}
