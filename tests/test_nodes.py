import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "singbox-manager"))
import node_manager as manager
from node_formats import parse_block, parse_numbered

KEY = base64.b64encode(bytes(range(32))).decode()
WG = f"""[Interface]
PrivateKey = {KEY}
Address = 10.1.0.2/32, fd00::2/128
[Peer]
PublicKey = {KEY}
Endpoint = [2001:db8::1]:51820
AllowedIPs = 0.0.0.0/0, ::/0
"""


class Formats(unittest.TestCase):
    def test_wg_defaults_and_dual_stack(self):
        node = parse_block(WG)
        self.assertEqual(node['mtu'], 1280)
        self.assertEqual(node['dns_server'], '1.1.1.1')
        self.assertEqual(node['peer_address'], '2001:db8::1')
        self.assertEqual(len(node['local_address']), 2)
        node['pre_shared_key'] = KEY
        config = manager.valid(node)
        peer = config['endpoints'][0]['peers'][0]
        self.assertEqual(peer['pre_shared_key'], KEY)
        self.assertEqual(peer['persistent_keepalive_interval'], 25)
        self.assertEqual(len(config['endpoints'][0]['address']), 2)

    def test_numbered_mixed_protocols_and_invalid_block(self):
        auth = base64.urlsafe_b64encode(b'aes-256-gcm:a:b').decode().rstrip('=')
        rows = parse_numbered(f'1\n{WG}\n2\nhttps://u:p%40x@example.com:443#label\n3\nss://{auth}@example.com:8388\n4\nu:p@127.0.0.1:1080\n5\nnot\nvalid')
        self.assertEqual([row[1]['protocol'] for row in rows[:4]], ['wg', 'https', 'shadowsocks', 'socks5'])
        self.assertEqual(rows[1][1]['password'], 'p@x')
        self.assertEqual(rows[2][1]['password'], 'a:b')
        self.assertIsNotNone(rows[4][2])

    def test_full_base64_ss_and_json(self):
        encoded = base64.b64encode(b'aes-256-gcm:pass@example.com:8388').decode()
        self.assertEqual(parse_block('ss://' + encoded)['server'], 'example.com')
        obj = {'type': 'socks', 'server': 'example.com', 'server_port': 1080, 'network': 'tcp'}
        self.assertFalse(parse_block(json.dumps(obj))['udp_enabled'])

    def test_invalid_fields_do_not_render(self):
        node = parse_block(WG)
        for change in ({'mtu': 0}, {'private_key': 'bad'}, {'peer_port': 70000}):
            with self.assertRaises(ValueError):
                manager.valid(dict(node, **change))


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(manager, 'DATABASE', self.root/'node-store.json'),
                        patch.object(manager, 'PROFILES', self.root),
                        patch.object(manager, 'CONFIG', self.root/'config.json'),
                        patch.object(manager, 'running', return_value=True),
                        patch.object(manager, 'health', return_value={'latency_ms': 50})]
        for item in self.patches:
            item.start()
        self.a = manager.new_record(parse_block('socks5://a.example:1080'))
        self.b = manager.new_record(parse_block('socks5://b.example:1080'))
        for record, latency in ((self.a, 200), (self.b, 20)):
            record.update(status='alive', latency_ms=latency, ip='192.0.2.1', location_zh='日本-东京')
        self.data = dict(version=1, nodes=[self.a, self.b], auto=True, active_id=self.a['id'], delete_after=3)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_healthy_active_node_is_sticky(self):
        with patch.object(manager, 'check_record'), patch.object(manager, 'activate') as activate:
            manager.scan(self.data, scheduled=True)
            activate.assert_not_called()
        self.assertEqual(self.data['active_id'], self.a['id'])

    def test_fastest_initial_selection_and_failed_connect_fallback(self):
        self.data['active_id'] = None
        with patch.object(manager, 'activate', side_effect=[False, True]) as activate:
            self.assertTrue(manager.connect_best(self.data))
            self.assertEqual([c.args[1]['id'] for c in activate.call_args_list], [self.b['id'], self.a['id']])

    def test_dead_active_triggers_next_node_only_in_auto_mode(self):
        self.a['status'] = 'dead'
        with patch.object(manager, 'check_record'), patch.object(manager, 'connect_best', return_value=True) as connect:
            manager.scan(self.data, scheduled=True)
            connect.assert_called_once_with(self.data, exclude=self.a['id'])
        self.data['auto'] = False
        with patch.object(manager, 'check_record'), patch.object(manager, 'connect_best') as connect:
            manager.scan(self.data, scheduled=True)
            connect.assert_not_called()

    def test_three_failed_rounds_then_delete(self):
        self.data.update(auto=False, active_id=None)
        self.data['nodes'] = [self.a]
        with patch.object(manager, 'test_record', side_effect=RuntimeError('unreachable')):
            for failures in (1, 2):
                manager.scan(self.data, scheduled=True)
                self.assertEqual(self.a['failures'], failures)
                self.assertEqual(len(self.data['nodes']), 1)
            manager.scan(self.data, scheduled=True)
        self.assertEqual(self.data['nodes'], [])

    def test_success_resets_and_manual_failures_do_not_prune(self):
        self.a['failures'] = 2
        with patch.object(manager, 'test_record', return_value={'latency_ms': 10}):
            manager.check_record(self.a, count_failure=True)
        self.assertEqual(self.a['failures'], 0)
        with patch.object(manager, 'test_record', side_effect=RuntimeError('unreachable')):
            manager.check_record(self.a, count_failure=False)
        self.assertEqual(self.a['failures'], 0)

    def test_failed_activation_preserves_existing_selected_node_and_profile(self):
        path = self.root/'socks5.json'
        manager.write_json(path, self.a['node'])
        previous = path.read_bytes()
        with patch.object(manager.subprocess, 'run') as command:
            command.return_value.returncode = 1
            self.assertFalse(manager.activate(self.data, self.b))
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.data['active_id'], self.a['id'])

    def test_import_deduplicates_and_private_database_mode(self):
        path = self.root/'input.txt'
        path.write_text('1\nsocks5://c.example:1080\n2\nsocks5://c.example:1080')
        with patch.object(manager, 'check_record'):
            manager.import_file(self.data, path)
        self.assertEqual(len(self.data['nodes']), 3)
        self.assertEqual(manager.DATABASE.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
