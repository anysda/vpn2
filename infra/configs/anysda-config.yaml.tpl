# anysda-vpn2 panel config — монтируется в контейнер как /etc/anysda/config.yaml
# Заполняет стадия 30-frontend (infra/vpn2/src/vpn2/containers/panel.py).
#
# Только логин admin: пароль панели не хранится нигде, кроме хэша в базе; остальное - из NUXT_* и /run/secrets.
# Логин — уже в двойных кавычках (JSON-строка = YAML-строка), иначе
# логин `12345678` читался числом, `!admin` тегом, а `x #y` обрезался.
admin:
  user: ${ADMIN_USER_YAML}
