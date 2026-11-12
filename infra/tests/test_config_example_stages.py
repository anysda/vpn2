"""Закомментированный блок config.example.yaml говорит, какую стадию запускать
после его правки: строка «После изменений: стадия NN-имя на ...». Без неё
человек меняет блок и не знает, что подхватит изменение. Блоки, которые
ставятся при любом deploy, строки не требуют."""
import inspect
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2 import containers  # noqa: E402
from vpn2.containers import compose  # noqa: E402
EXAMPLE = ROOT / 'config.example.yaml'
SITE = ROOT / 'infra' / 'ansible' / 'site.yml'
ROLES = ROOT / 'infra' / 'ansible' / 'roles'

BLOCK = re.compile(r'^# ([a-z_][a-z0-9_]*):\s*$')
CHILD = re.compile(r'^# {2,}\S')
TOP = re.compile(r'^([a-z_][a-z0-9_]*):')
AFTER = 'После изменений'
STAGE = re.compile(r'\b(\d\d-[a-z0-9]+(?:-[a-z0-9]+)*)\b')
FULL_DEPLOY = re.compile(r'(при любом|полн\w*) deploy')


def paragraphs(lines):
    """Куски файла между пустыми строками: [(номер первой строки, строки)]."""
    chunks, start, chunk = [], 1, []
    for number, line in enumerate(lines, 1):
        if line.strip():
            if not chunk:
                start = number
            chunk.append(line)
        elif chunk:
            chunks.append((start, chunk))
            chunk = []
    if chunk:
        chunks.append((start, chunk))
    return chunks


def commented_blocks(lines):
    """{ключ: (номер строки ключа, строки его абзаца)} для ключей верхнего
    уровня, которые есть в примере только закомментированными."""
    live = {match.group(1) for line in lines if (match := TOP.match(line))}
    blocks = {}
    for start, chunk in paragraphs(lines):
        for offset, line in enumerate(chunk):
            match = BLOCK.match(line)
            following = chunk[offset + 1] if offset + 1 < len(chunk) else ''
            if match and CHILD.match(following) and match.group(1) not in live:
                blocks[match.group(1)] = (start + offset, chunk)
    return blocks


class ConfigExampleStagesTests(unittest.TestCase):
    def setUp(self):
        self.lines = EXAMPLE.read_text(encoding='utf-8').splitlines()
        self.blocks = commented_blocks(self.lines)
        # Стадия -> имена ролей её плея.
        plays = yaml.safe_load(SITE.read_text(encoding='utf-8'))
        self.stages = {play['tags'][0]: {role['role'] if isinstance(role, dict) else role for role in play['roles']}
                       for play in plays}
        # Стадия -> служба роли containers (шаг пакета vpn2.containers).
        self.services = {play['tags'][0]: role['containers_service'] for play in plays for role in play['roles']
                         if isinstance(role, dict) and 'containers_service' in role}

    def test_parser_sees_commented_blocks(self):
        for key in ('alerts', 'failover', 'wireguard', 'telegram'):
            self.assertIn(key, self.blocks)
        for key in ('entry', 'exits', 'backup', 'route'):
            self.assertNotIn(key, self.blocks)

    def test_every_commented_block_names_stage_after_change(self):
        missing = [f'{key} (config.example.yaml:{number})'
                   for key, (number, chunk) in self.blocks.items()
                   if not FULL_DEPLOY.search(' '.join(chunk))
                   and not any(AFTER in line for line in chunk)]
        self.assertEqual(missing, [], 'нет строки «# После изменений: стадия …»')

    def test_named_stages_exist(self):
        named = [(number, stage) for number, line in enumerate(self.lines, 1) if AFTER in line
                 for stage in STAGE.findall(line)]
        self.assertTrue(named)
        absent = [f'{stage} (config.example.yaml:{number})' for number, stage in named
                  if stage not in self.stages]
        self.assertEqual(absent, [], 'стадии нет в infra/ansible/site.yml')

    def test_alerts_names_stage_writing_thresholds(self):
        alert = re.compile(r'\bALERT_[A-Z_]+')
        # Стадию ведёт роль: пороги читает роль, названная в плее.
        roles = {path.parent.parent.name for path in ROLES.glob('*/defaults/main.yml')
                 if alert.search(path.read_text(encoding='utf-8'))}
        writers = {stage for stage, named in self.stages.items() if roles & named}
        # Службы entry пороги передаёт шаг пакета: функции compose, которые он зовёт.
        for stage, service in self.services.items():
            step = inspect.getsource(containers.STAGES[service])
            called = re.findall(r'\bcompose\.(\w+)\(', step)
            if any(alert.search(inspect.getsource(getattr(compose, name))) for name in called):
                writers.add(stage)
        self.assertTrue(writers, 'ни одна стадия не пишет ALERT_*')
        _, chunk = self.blocks['alerts']
        named = {stage for line in chunk if AFTER in line for stage in STAGE.findall(line)}
        self.assertTrue(named & writers,
                        f'у alerts: «После изменений» называет {sorted(named)}, '
                        f'ALERT_* пишет {sorted(writers)}')


if __name__ == '__main__':
    unittest.main()
