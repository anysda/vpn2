"""Инструменты не принимают вывод без метки ssh за ответ команды.

Метка печатается до команды: если её нет, shell узла не дошёл до команды
(.bashrc root делает exit или exec). node() из emergency-accept.sh, ssh_node()
из ci-stand.sh и sh() из speedtest.py тогда падают с причиной, а не отдают
пустоту или текст .bashrc с кодом 0.
"""
import os
import runpy
import unittest
from unittest import mock

from test_deploy import ROOT
from test_ssh_login_noise import Node, tool_function

REASON = 'не дошёл до команды'
SSH_DOWN = 'ssh: connect to host 203.0.113.7 port 22: Connection refused'
FAKE_SSH_DOWN = f'#!/bin/bash\necho "{SSH_DOWN}" >&2\nexit 255\n'
FUNCTIONS = (tool_function('infra/tools/emergency-accept.sh', 'node')
             + tool_function('infra/tools/ci-stand.sh', 'ssh_node')
             + '''declare -A IP=([exit1]=203.0.113.7)
SSH_KEY='' KNOWN_HOSTS='' STAND_DIR=$PWD
''')
CALLS = (('node', "node exit1 '{}'"), ('ssh_node', "ssh_node 203.0.113.7 '{}'"))


def ssh_down(node):
    (node.root / 'bin' / 'ssh').write_text(FAKE_SSH_DOWN)


class ToolNoMarkTests(unittest.TestCase):
    def run_tool(self, node, call, command):
        return node.bash(FUNCTIONS + call.format(command) + '\n')

    def test_login_exit_before_command_fails_with_reason(self):
        for noise in ('NOISE\\n', ''):
            for name, call in CALLS:
                with self.subTest(tool=name, noise=noise), Node(noise) as node:
                    node.env['FAKE_LOGIN_EXIT'] = '1'
                    result = self.run_tool(node, call, 'echo leader 3')
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(REASON, result.stderr)
                    self.assertEqual(result.stdout, '')

    def test_marked_output_after_noise_keeps_command_output_and_code(self):
        for name, call in CALLS:
            with self.subTest(tool=name), Node('NOISE\\nmotd\\n') as node:
                result = self.run_tool(node, call, 'echo leader 3')
                self.assertEqual((result.returncode, result.stdout), (0, 'leader 3\n'), result.stderr)
                result = self.run_tool(node, call, 'echo before; exit 5')
                self.assertEqual((result.returncode, result.stdout), (5, 'before\n'), result.stderr)
                self.assertNotIn(REASON, result.stderr)

    def test_connection_failure_keeps_ssh_code_and_message(self):
        for name, call in CALLS:
            with self.subTest(tool=name), Node('') as node:
                ssh_down(node)
                result = self.run_tool(node, call, 'echo leader 3')
                self.assertEqual(result.returncode, 255)
                self.assertIn(SSH_DOWN, result.stderr)
                self.assertNotIn(REASON, result.stderr)
                self.assertEqual(result.stdout, '')

    def test_marked_command_without_output_is_success(self):
        for name, call in CALLS:
            with self.subTest(tool=name), Node('NOISE\\n') as node:
                result = self.run_tool(node, call, 'true')
                self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)
                self.assertNotIn(REASON, result.stderr)


class SpeedtestNoMarkTests(unittest.TestCase):
    sh = staticmethod(runpy.run_path(str(ROOT / 'infra/tools/speedtest.py'))['sh'])
    ROUTES = (('direct', {}), ('via hub', {'via': 'hub'}))

    def test_login_exit_before_command_raises_with_reason(self):
        for noise in ('NOISE\\n', ''):
            for route, kwargs in self.ROUTES:
                with self.subTest(route=route, noise=noise), Node(noise) as node, \
                        mock.patch.dict(os.environ, node.env):
                    os.environ['FAKE_LOGIN_EXIT'] = '1'
                    with self.assertRaisesRegex(RuntimeError, REASON):
                        self.sh('exit1', 'echo leader 3', **kwargs)

    def test_marked_output_after_noise_keeps_command_output_and_code(self):
        for route, kwargs in self.ROUTES:
            with self.subTest(route=route), Node('NOISE\\nmotd\\n') as node, \
                    mock.patch.dict(os.environ, node.env):
                self.assertEqual(self.sh('exit1', 'echo leader 3', **kwargs), 'leader 3\n')
                with self.assertRaisesRegex(RuntimeError, 'rc=5') as raised:
                    self.sh('exit1', 'exit 5', **kwargs)
                self.assertNotIn(REASON, str(raised.exception))

    def test_connection_failure_keeps_ssh_code_and_message(self):
        for route, kwargs in self.ROUTES:
            with self.subTest(route=route), Node('') as node, \
                    mock.patch.dict(os.environ, node.env):
                ssh_down(node)
                with self.assertRaisesRegex(RuntimeError, 'rc=255') as raised:
                    self.sh('exit1', 'echo leader 3', **kwargs)
                self.assertIn(SSH_DOWN, str(raised.exception))
                self.assertNotIn(REASON, str(raised.exception))

    def test_marked_command_without_output_is_success(self):
        for route, kwargs in self.ROUTES:
            with self.subTest(route=route), Node('NOISE\\n') as node, \
                    mock.patch.dict(os.environ, node.env):
                self.assertEqual(self.sh('exit1', 'true', **kwargs), '')


if __name__ == '__main__':
    unittest.main()
