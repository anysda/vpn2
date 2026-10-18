"""The deploy exit code is the verification result, including incomplete runs."""
import json
import subprocess
import unittest

from test_deploy_verify import orchestrate


class VerifyResultTests(unittest.TestCase):
    def test_incomplete_result_survives_both_host_orders_and_entrypoints(self):
        good = subprocess.CompletedProcess([], 0, '[node] verify done\n')
        unknown = subprocess.CompletedProcess([], 2, '[node] ? внешний путь — не проверено: нет точки\n')
        for nodes in ((good, unknown), (unknown, good), (unknown, unknown)):
            for mode in ('single', 'full'):
                with self.subTest(nodes=[n.returncode for n in nodes], mode=mode):
                    result = orchestrate(*nodes, mode=mode)
                    print(f'{mode}: nodes={[n.returncode for n in nodes]}, rc={result.returncode}')
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn('не проверено: нет точки', result.stdout)
                    if mode == 'full':
                        # Установка прошла: бот получает итог с пометкой «не всё измерено».
                        payload = json.loads(result.event.split("-d '", 1)[1].split("'", 1)[0])
                        self.assertTrue(payload['verify_unmeasured'])


if __name__ == '__main__':
    unittest.main()
