"""У каждой функции infra/lib/anysda-svc.sh есть вызов из продукта: тест вызовом не считается."""
import re
import subprocess

from nodevars_support import ROOT

LIB = 'infra/lib/anysda-svc.sh'
SHELL_CARRIERS = ('.sh', '.yml', '.yaml', '.j2', '.tpl', '.py', '.service')
TESTS = ('infra/tests/', 'infra/vpn2/tests/', 'app/e2e/')


def product_files():
    names = subprocess.run(['git', '-C', str(ROOT), 'ls-files'], text=True, capture_output=True, check=True).stdout
    return [name for name in names.splitlines()
            if name.endswith(SHELL_CARRIERS) and not name.startswith(TESTS)]


def code_lines(text):
    return [line for line in text.splitlines() if not line.lstrip().startswith('#')]


def test_every_function_has_a_product_caller():
    library = (ROOT / LIB).read_text(encoding='utf-8')
    functions = re.findall(r'^([a-z_0-9]+)\(\)', library, flags=re.M)
    assert functions
    texts = {name: '\n'.join(code_lines((ROOT / name).read_text(encoding='utf-8', errors='replace')))
             for name in product_files()}
    dead = []
    for function in functions:
        word = re.compile(rf'(?<![\w-]){function}(?![\w-])')
        calls = sum(len(word.findall(text)) for text in texts.values())
        # Одно вхождение - само определение в библиотеке.
        if calls < 2:
            dead.append(function)
    assert dead == [], f'функции без вызова из продукта: {dead}'
