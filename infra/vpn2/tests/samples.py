"""Образцы config.yaml для сверки нового парсера со старым (config2env.parse_yaml).

Источники собираются без правки самих источников:
  example - config.example.yaml из корня репы;
  tests   - строковые константы infra/tests/*.py, где с нулевого отступа
            начинается строка entry:, exits:, admin: или panel:;
  wizard  - config.yaml, который пишет мастер setup.sh (подготовка как в
            infra/tests/setup-compat.sh).
"""
import ast
import importlib.util
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOP_KEY = re.compile(r'^(entry|exits|admin|panel):', re.MULTILINE)

WIZARD_STUBS = r"""
ip() { return 0; }
dig() { return 0; }
curl() {
  case "$*" in
    *api.ipify.org*) printf '192.0.2.1' ;;
    *ip-api.com*) printf '{}' ;;
    *) return 1 ;;
  esac
}
export -f ip dig curl
printf '%s\n' 1 test-entry-password 198.51.100.2 test-exit-password de \
  '' '' '' n n n n y | bash "$1/setup.sh" > "$1/output" 2>&1
"""


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
        fixture = Path(tmp)
        (fixture / 'infra/lib').mkdir(parents=True)
        shutil.copy(ROOT / 'setup.sh', fixture)
        for name in ('config2env.py', 'hy2_bandwidth.py'):
            shutil.copy(ROOT / 'infra/lib' / name, fixture / 'infra/lib')
        subprocess.run(['bash', '-c', WIZARD_STUBS, 'bash', str(fixture)], check=False,
                       stdin=subprocess.DEVNULL, timeout=120)
        config = fixture / 'config.yaml'
        return {'wizard': config.read_text()} if config.exists() else {}


def collect():
    """{источник: {имя образца: текст}}."""
    return {'example': example_samples(), 'tests': tests_samples(), 'wizard': wizard_samples()}
