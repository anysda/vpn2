"""Ошибка restore с кодом выхода bash-скрипта; общая для шагов восстановления."""


class RestoreError(Exception):
    """Шаг restore не удался; code - код выхода anysda-restore.sh, message - его текст как в stderr."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
