"""Every zbarimg call in the repo decodes QR only.

Without -Sdisable -Sqrcode.enable zbarimg tries every symbology and now and
then reads a linear barcode out of the QR pattern as a second line.
"""

import ast
import io
from pathlib import Path
import shlex
import tokenize
import unittest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {'.git', 'node_modules', '.lane', 'dist', 'build', '.venv'}
NEEDED = {'disable', 'qrcode.enable'}
SH_COMMAND_START = {None, '|', '||', '&&', ';', '(', '$(', 'then', 'do', 'else', '!', 'exec'}


def symbol_settings(args):
    settings = set()
    args = list(args)
    for index, arg in enumerate(args):
        if arg in ('-S', '--set'):
            value = args[index + 1] if index + 1 < len(args) else ''
        elif arg.startswith('--set='):
            value = arg[len('--set='):]
        elif arg.startswith('-S'):
            value = arg[2:]
        else:
            continue
        settings.add(value[:-2] if value.endswith('=1') else value)
    return settings


def python_calls(text):
    tokens = [tok for tok in tokenize.generate_tokens(io.StringIO(text).readline)
              if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT)]
    for index, tok in enumerate(tokens):
        if tok.type != tokenize.STRING or tok.string[0] not in '\'"':
            continue
        if ast.literal_eval(tok.string) != 'zbarimg':
            continue
        if index >= 2 and tokens[index - 1].string == '(' and tokens[index - 2].string == 'which':
            continue
        args, depth = [], 0
        for later in tokens[index + 1:]:
            if later.string in '([{':
                depth += 1
            elif later.string in ')]}':
                depth -= 1
                if depth < 0:
                    break
            elif later.type == tokenize.STRING and later.string[0] in '\'"':
                args.append(ast.literal_eval(later.string))
        yield tok.start[0], args


def ts_regex_end(text, index):
    in_class = False
    index += 1
    while index < len(text) and text[index] != '\n':
        char = text[index]
        if char == '\\':
            index += 1
        elif char == '[':
            in_class = True
        elif char == ']':
            in_class = False
        elif char == '/' and not in_class:
            return index + 1
        index += 1
    return None


def ts_tokens(text):
    index, line, previous = 0, 1, None
    while index < len(text):
        char = text[index]
        regex_end = None
        if char == '/' and not text.startswith(('//', '/*'), index) and (
                previous is None or previous in '([{,=:!&|?;+-*%<>~^'):
            regex_end = ts_regex_end(text, index)
        if regex_end:
            yield 'other', text[index:regex_end], line
            index, previous = regex_end, 'regex'
        elif text.startswith('//', index):
            index = text.find('\n', index) if '\n' in text[index:] else len(text)
        elif text.startswith('/*', index):
            end = text.find('*/', index + 2)
            end = len(text) if end < 0 else end + 2
            line += text.count('\n', index, end)
            index = end
        elif char in '\'"`':
            end, value = index + 1, []
            while end < len(text) and text[end] != char:
                if text[end] == '\\':
                    end += 1
                value.append(text[end] if end < len(text) else '')
                end += 1
            yield 'string', ''.join(value), line
            line += text.count('\n', index, end)
            index, previous = end + 1, 'string'
        else:
            if char == '\n':
                line += 1
            elif not char.isspace():
                yield 'punct' if char in '()[]{}' else 'other', char, line
                previous = char if not (char.isalnum() or char in '_$') else 'word'
            index += 1


def ts_calls(text):
    tokens = list(ts_tokens(text))
    for index, (kind, value, line) in enumerate(tokens):
        if kind != 'string' or value != 'zbarimg':
            continue
        args, depth = [], 0
        for later_kind, later, _line in tokens[index + 1:]:
            if later_kind == 'punct' and later in '([{':
                depth += 1
            elif later_kind == 'punct':
                depth -= 1
                if depth < 0:
                    break
            elif later_kind == 'string':
                args.append(later)
        yield line, args


def sh_calls(text):
    for number, raw in enumerate(text.replace('\\\n', ' \n').splitlines(), 1):
        lexer = shlex.shlex(raw, posix=True, punctuation_chars=True)
        try:
            words = list(lexer)
        except ValueError:
            continue
        if 'command' in words and '-v' in words:
            continue
        previous = None
        for index, word in enumerate(words):
            if word == 'zbarimg' and previous in SH_COMMAND_START:
                args = []
                for later in words[index + 1:]:
                    if later in ('|', '||', '&&', ';', ')', '&'):
                        break
                    args.append(later)
                yield number, args
            previous = None if '=' in word and previous in SH_COMMAND_START else word


PARSERS = {'.py': python_calls, '.ts': ts_calls, '.sh': sh_calls}


def offenders(path, text):
    found = []
    for line, args in PARSERS[path.suffix](text):
        if not NEEDED <= symbol_settings(args):
            found.append(line)
    return found


def repo_files():
    for path in sorted(ROOT.rglob('*')):
        if path.suffix in PARSERS and path.is_file() and not SKIP_DIRS & set(path.relative_to(ROOT).parts):
            if path.resolve() != Path(__file__).resolve():
                yield path


class ZbarimgQrOnlyTests(unittest.TestCase):
    def test_every_zbarimg_call_enables_only_qrcode(self):
        calls, bad = 0, []
        for path in repo_files():
            text = path.read_text(errors='replace')
            if 'zbarimg' not in text:
                continue
            calls += len(list(PARSERS[path.suffix](text)))
            bad += [f'{path.relative_to(ROOT)}:{line}' for line in offenders(path, text)]
        self.assertGreater(calls, 0, 'scanner found no zbarimg calls at all')
        self.assertEqual(bad, [], 'zbarimg without -Sdisable -Sqrcode.enable')


if __name__ == '__main__':
    unittest.main()
