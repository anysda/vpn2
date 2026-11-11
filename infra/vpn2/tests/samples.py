"""Образцы config.yaml для сверки нового парсера со старым (config2env.parse_yaml).

Источники собираются без правки самих источников:
  example - config.example.yaml из корня репы;
  tests   - строковые константы infra/tests/*.py, где с нулевого отступа
            начинается строка entry:, exits:, admin: или panel:;
  wizard  - config.yaml, который пишет мастер setup.sh на ответы эталона
            fixtures/setup/minimal.
"""
import ast
import importlib.util
import re
import tempfile
from pathlib import Path

from setup_support import FIXTURES, prepare, run

ROOT = Path(__file__).resolve().parents[3]
TOP_KEY = re.compile(r'^(entry|exits|admin|panel):', re.MULTILINE)

WIZARD_ANSWERS = (FIXTURES / 'minimal/answers').read_text()
WIZARD_STUBS = {'ipify': '192.0.2.1', 'geo-198.51.100.2': '{}'}


def load_oracle():
    spec = importlib.util.spec_from_file_location('config2env', ROOT / 'infra/lib/config2env.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _constant_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and all(
            isinstance(v, ast.Constant) and isinstance(v.value, str) for v in node.values):
        return ''.join(v.value for v in node.values)
    return None


def example_samples():
    return {'example': (ROOT / 'config.example.yaml').read_text()}


def tests_samples():
    found = {}
    for path in sorted((ROOT / 'infra/tests').glob('*.py')):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            text = _constant_text(node)
            if text is not None and TOP_KEY.search(text):
                found[f'tests:{path.name}:{node.lineno}'] = text
    return found


def wizard_samples():
    with tempfile.TemporaryDirectory() as tmp:
        root = prepare(tmp, stubs=WIZARD_STUBS)
        run(root, WIZARD_ANSWERS, timeout=120)
        config = root / 'config.yaml'
        return {'wizard': config.read_text()} if config.exists() else {}


def collect():
    """{источник: {имя образца: текст}}."""
    return {'example': example_samples(), 'tests': tests_samples(), 'wizard': wizard_samples()}
