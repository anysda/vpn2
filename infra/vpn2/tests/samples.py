"""Образцы config.yaml из репы для проверок переменных узлов.

Источники собираются без правки самих источников:
  example - config.example.yaml из корня репы;
  tests   - строковые константы infra/tests/*.py и infra/vpn2/tests/*.py, где с нулевого отступа
            начинается строка entry:, exits:, admin: или panel:.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOP_KEY = re.compile(r'^(entry|exits|admin|panel):', re.MULTILINE)


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
    for path in sorted([*(ROOT / 'infra/tests').glob('*.py'), *(ROOT / 'infra/vpn2/tests').glob('*.py')]):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            text = _constant_text(node)
            if text is not None and TOP_KEY.search(text):
                found[f'tests:{path.relative_to(ROOT)}:{node.lineno}'] = text
    return found


def collect():
    """{источник: {имя образца: текст}}."""
    return {'example': example_samples(), 'tests': tests_samples()}
