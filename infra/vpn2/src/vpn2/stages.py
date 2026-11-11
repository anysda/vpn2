"""Стадии деплоя: имена, узлы и порядок читаются из infra/ansible/site.yml.

site.yml - единственный список стадий: один плей на стадию, тег плея - её имя, hosts - группа
узлов (all, entry, exits), порядок плеев - порядок полного деплоя. Разбор без PyYAML: проверка
аргументов `./deploy.sh <стадия> <группа>` идёт до сборки venv, на голой системе.
"""
import re
import sys
from pathlib import Path
from typing import NamedTuple

SITE = Path(__file__).resolve().parents[3] / 'ansible/site.yml'
ENTRY = 'ru'
GROUPS = ('all', 'entry', 'exits')
KEY = re.compile(r'^(- |  )([a-z_]+):\s*(.*?)\s*$')
TAGS = re.compile(r'^\[\s*([A-Za-z0-9-]+)\s*\]$')


class StageError(Exception):
    """Отказ оператору: текст уходит в строку FATAL деплоя."""


class UnknownStage(StageError):
    pass


class Stage(NamedTuple):
    name: str
    hosts: str

    def has(self, node):
        """Есть ли стадия у узла: ru - entry, остальные теги - экзиты."""
        return self.hosts == 'all' or (self.hosts == 'entry') == (node == ENTRY)


def parse(text):
    """Плеи site.yml по порядку. Формат узкий намеренно: его же проверяет тест через PyYAML."""
    plays, current = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#') or line == '---':
            continue
        key = KEY.match(line)
        if key and key[1] == '- ':
            current = {}
            plays.append(current)
        if current is None:
            raise ValueError(f'site.yml:{number}: строка вне плея')
        if key:
            current[key[2]] = key[3]
    stages = []
    for play in plays:
        tag = TAGS.match(play.get('tags', ''))
        if not tag or play.get('hosts') not in GROUPS:
            raise ValueError(f'site.yml: у плея «{play.get("name", "?")}» нужны tags: [стадия] и hosts: all|entry|exits')
        stages.append(Stage(tag[1], play['hosts']))
    names = [stage.name for stage in stages]
    if len(set(names)) != len(names):
        raise ValueError('site.yml: стадия записана дважды')
    return stages


def load(site=SITE):
    return parse(Path(site).read_text(encoding='utf-8'))


def find(stages, name):
    for stage in stages:
        if stage.name == name:
            return stage
    raise UnknownStage(f'нет такой стадии: {name} (список: ./deploy.sh --help)')


def nodes(stages, name, group, *, refuse_empty=False):
    """Узлы группы, у которых стадия есть. Пустой итог при refuse_empty - отказ оператору."""
    stage = find(stages, name)
    kept = [node for node in group if stage.has(node)]
    if group and not kept and refuse_empty:
        if stage.hosts == 'exits':
            raise StageError(f'{name} - стадия экзитов, на entry её нет: группа foreign, all или тег экзита')
        raise StageError(f'{name} - стадия entry, на экзитах её нет: группа ru или all')
    return kept


def main(argv):
    """nodes [--site ФАЙЛ] [--refuse-empty] СТАДИЯ [УЗЕЛ...] | list [--site ФАЙЛ]"""
    args = list(argv)
    site, refuse = SITE, False
    command = args.pop(0) if args else ''
    while args and args[0].startswith('--'):
        option = args.pop(0)
        if option == '--site' and args:
            site = args.pop(0)
        elif option == '--refuse-empty':
            refuse = True
        else:
            print(main.__doc__, file=sys.stderr)
            return 2
    stages = load(site)
    try:
        if command == 'list' and not args:
            print('\n'.join(f'{stage.name} {stage.hosts}' for stage in stages))
        elif command == 'nodes' and args:
            print(' '.join(nodes(stages, args[0], args[1:], refuse_empty=refuse)))
        else:
            print(main.__doc__, file=sys.stderr)
            return 2
    except UnknownStage as error:
        print(error, file=sys.stderr)
        return 3
    except StageError as error:
        print(error, file=sys.stderr)
        return 4
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
