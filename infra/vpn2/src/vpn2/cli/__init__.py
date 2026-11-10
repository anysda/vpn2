"""Консольная команда vpn2 с авто-обнаружением подкоманд.

Каждый модуль cli/<имя>.py с функцией main(argv) - команда `vpn2 <имя>`;
подчёркивание в имени становится дефисом. Имена с `_` в начале - не команды.
"""

import importlib
import pkgutil
import sys


def _names():
    return sorted(
        m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")
    )


def _load(name):
    return importlib.import_module(f"{__name__}.{name}")


def _commands():
    """Словарь команда -> имя модуля, только модули, где импорт даёт main."""
    found = {}
    for name in _names():
        try:
            mod = _load(name)
        except (Exception, SystemExit):
            # сломанный модуль остаётся командой: ошибку покажет её вызов
            found[name.replace("_", "-")] = name
            continue
        if callable(getattr(mod, "main", None)):
            found[name.replace("_", "-")] = name
    return found


def _usage(cmds):
    return "Команды: " + ", ".join(sorted(cmds)) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    cmds = _commands()
    if not argv:
        sys.stderr.write("vpn2: нужна команда\n" + _usage(cmds))
        return 2
    if argv[0] in ("-h", "--help"):
        sys.stdout.write("Использование: vpn2 <команда> [аргументы]\n" + _usage(cmds))
        return 0
    name = cmds.get(argv[0])
    if name is None:
        sys.stderr.write(f"vpn2: неизвестная команда {argv[0]}\n" + _usage(cmds))
        return 2
    try:
        main = _load(name).main
    except (Exception, SystemExit) as exc:
        sys.stderr.write(f"vpn2 {argv[0]}: {exc!r}\n")
        return 1
    try:
        rc = main(argv[1:])
    except Exception as exc:
        sys.stderr.write(f"vpn2 {argv[0]}: {exc!r}\n")
        return 1
    return rc or 0
