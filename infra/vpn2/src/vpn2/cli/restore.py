"""Команда `vpn2 restore`: восстановить узел из зашифрованного архива."""
import sys

from vpn2.backup import restore

USAGE = '''Использование: vpn2 restore [АРХИВ|latest] [--force|-f] [--dry-run] [--allow-incompatible]

Восстановить узел из архива (по умолчанию - самый свежий, локальный или из S3).
  --force, -f            перетереть непустую базу с клиентами
  --dry-run              только назвать архив, ничего не менять
  --allow-incompatible   восстановить архив другой версии формата или схемы БД
'''


def main(argv):
    if argv and argv[0] in ('-h', '--help'):
        sys.stdout.write(USAGE)
        return 0
    return restore.main(argv)
