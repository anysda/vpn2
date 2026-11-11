"""Контейнеры entry под compose: стадии установки и откат панели."""
from vpn2.containers import engine, monitoring, panel, telegram
from vpn2.containers.compose import ComposeError
from vpn2.containers.transaction import Transaction, TransactionError

STAGES = {'monitoring': monitoring.run, 'telegram': telegram.run, 'panel': panel.run}
ERRORS = (ComposeError, engine.EngineError, TransactionError, panel.StageError)


def run(stage, environ):
    """Стадия целиком; вернуть, менялся ли узел."""
    return STAGES[stage](environ)


def rollback():
    """Вернуть прежний образ панели; занятый замок деплоя - отказ."""
    transaction = Transaction(engine.Docker())
    transaction.lock()
    return transaction.rollback()
