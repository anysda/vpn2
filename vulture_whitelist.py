"""Имена, которые зовёт не наш код: vulture их не видит."""
_ = None

_.do_POST  # http.server: обработчик метода запроса (infra/tests/backup-restore.py)
_.log_message  # http.server: журнал запросов обработчика (backup-restore.py, test_emergency_parts.py)
_.read_timeout  # telegram.request.BaseRequest: свойство транспорта (telegram/tests/harness.py)
_.initialize  # telegram.request.BaseRequest: запуск транспорта (telegram/tests/harness.py)
_.row_factory  # sqlite3.Connection: строки по именам столбцов (vpn2/backup/revoke.py)
_.backup_list  # vpn2.cli.COMMANDS по имени: vpn2 backup-list из роли backup
_.nft  # vpn2.cli.COMMANDS по имени: vpn2 nft yt-quic из anysda-yt-quic-refresh.sh
