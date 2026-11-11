"""vpn2 containers monitoring|telegram|panel|rollback - контейнеры entry под compose."""

import argparse
import os
import subprocess
import sys

from vpn2.containers import engine, monitoring, panel, telegram
from vpn2.containers.compose import ComposeError
from vpn2.containers.transaction import Transaction, TransactionError

STAGES = {'monitoring': monitoring.run, 'telegram': telegram.run, 'panel': panel.run}


def rollback():
    """Вернуть прежний образ панели; занятый замок деплоя - отказ."""
    transaction = Transaction(engine.Docker())
    transaction.lock()
    return 0 if transaction.rollback() else 1


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 containers')
    parser.add_argument('command', choices=[*STAGES, 'rollback'])
    args = parser.parse_args(argv)
    try:
        if args.command == 'rollback':
            return rollback()
        if os.environ.get('HOST_TAG') != 'ru':
            print(f'[{os.environ.get("HOST_TAG")}] {args.command}: только для entry - пропуск')
            print('changed=0')
            return 0
        changed = STAGES[args.command](os.environ)
    except (ComposeError, engine.EngineError, TransactionError, panel.StageError) as error:
        if str(error):
            print(f'vpn2 containers {args.command}: {error}', file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f'vpn2 containers {args.command}: {error.cmd[0]}: код {error.returncode}', file=sys.stderr)
        return 1
    print(f'changed={int(bool(changed))}')
    return 0
