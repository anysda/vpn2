"""Закомментированный блок config.example.yaml говорит, какую стадию запускать
после его правки: строка «После изменений: стадия NN-имя на ...». Без неё
человек меняет блок и не знает, что подхватит изменение. Блоки, которые
ставятся при любом deploy, строки не требуют."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'config.example.yaml'
SCRIPTS = ROOT / 'infra' / 'scripts'

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
                  if not (SCRIPTS / f'{stage}.sh').is_file()]
        self.assertEqual(absent, [], 'стадии нет в infra/scripts')

    def test_alerts_names_stage_writing_thresholds(self):
        writers = {path.stem for path in SCRIPTS.glob('*.sh')
                   if re.search(r'\bALERT_[A-Z_]+', path.read_text(encoding='utf-8'))}
        self.assertTrue(writers, 'ни одна стадия не пишет ALERT_*')
        _, chunk = self.blocks['alerts']
        named = {stage for line in chunk if AFTER in line for stage in STAGE.findall(line)}
        self.assertTrue(named & writers,
                        f'у alerts: «После изменений» называет {sorted(named)}, '
                        f'ALERT_* пишет {sorted(writers)}')


if __name__ == '__main__':
    unittest.main()
