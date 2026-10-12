import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'singbox-manager'
ROOT = LIB.parent


class EncodingRegression(unittest.TestCase):
    def bad_environment(self, encoding='latin-1', utf8='0'):
        env = os.environ.copy()
        env.update(LC_ALL='C', LANG='C', PYTHONCOERCECLOCALE='0', PYTHONUTF8=utf8)
        if encoding:
            env['PYTHONIOENCODING'] = encoding
        else:
            env.pop('PYTHONIOENCODING', None)
        return env

    def run_python(self, path, *args, input_text='', encoding='latin-1', utf8='0'):
        return subprocess.run([sys.executable, str(path), *args],
                              input=input_text.encode('utf-8'), capture_output=True,
                              env=self.bad_environment(encoding, utf8), timeout=15)

    def test_chinese_menu_names_input_and_json_survive_legacy_encodings(self):
        # Run an actual interactive menu with an isolated database and lock.
        # Re-entering Python must not lose arguments or execute actions twice.
        for encoding, utf8 in [('latin-1', '0'), ('ascii', '0'), (None, '0'), ('latin-1', '1')]:
            with self.subTest(encoding=encoding, utf8=utf8), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                harness = folder / 'menu.py'
                harness.write_text(
                    f'import sys\nsys.path.insert(0, {str(LIB)!r})\n'
                    'from utf8_runtime import ensure_utf8\nensure_utf8()\n'
                    'from pathlib import Path\nimport node_manager as m\n'
                    'm.PROFILES = Path(__file__).parent\n'
                    'm.DATABASE = m.PROFILES / "node-store.json"\n'
                    'm.LOCK = m.PROFILES / "test.lock"\nm.main()\n', encoding='utf-8')
                database = folder / 'node-store.json'
                database.write_text(json.dumps({'version': 1, 'auto': False, 'active_id': None,
                    'nodes': [{'id': 'fixture', 'name': '新加坡-测试节点😀', 'node': {'protocol': 'socks5'},
                               'status': 'alive', 'failures': 0, 'latency_ms': 12}]
                    }, ensure_ascii=False), encoding='utf-8')
                result = self.run_python(harness, 'menu', input_text='9\n1\n东京测试😀\n0\n',
                                         encoding=encoding, utf8=utf8)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
                self.assertIn('多节点管理', result.stdout.decode('utf-8'))
                self.assertIn('新加坡-测试节点😀', result.stdout.decode('utf-8'))
                self.assertIn('东京测试😀', result.stdout.decode('utf-8'))
                self.assertEqual(json.loads(database.read_text(encoding='utf-8'))['nodes'][0]['custom_name'], '东京测试😀')
                summary = json.loads((folder/'node-summary.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['nodes'][0]['name'], '东京测试😀')

    def test_all_cli_error_handlers_use_utf8_without_secondary_traceback(self):
        for script, args, expected in [
            ('edit_node.py', ['invalid', 'unused'], '编辑失败：未知协议'),
            ('render.py', [], '配置错误：用法'),
            ('probe.py', [], '节点测试失败：用法'),
            ('transparent.py', [], '透明代理规则错误：用法'),
            ('telegram_monitor.py', [], 'Telegram 监测错误：用法'),
        ]:
            with self.subTest(script=script):
                result = self.run_python(LIB/script, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr.decode('utf-8'))
                self.assertNotIn(b'Traceback', result.stderr)

    def test_shell_child_python_overrides_inherited_latin1(self):
        result = subprocess.run(['bash', '-c',
            """source <(head -n -1 "$1"); python3 -c 'import sys; print("中文菜单😀"); assert sys.flags.utf8_mode'""",
            'test', str(ROOT/'singbox.sh')], capture_output=True,
            env=self.bad_environment(), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        self.assertEqual(result.stdout.decode('utf-8').strip(), '中文菜单😀')


if __name__ == '__main__':
    unittest.main()
