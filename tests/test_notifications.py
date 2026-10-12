import copy
import json
from pathlib import Path
import sys
import tempfile
import time
from contextlib import nullcontext
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'singbox-manager'))
import node_manager as manager
import telegram_monitor as telegram
from node_formats import parse_block
from node_notifications import public_snapshot, split_message, summary_lines


class Notifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(manager, 'DATABASE', self.root/'node-store.json'),
                        patch.object(manager, 'PROFILES', self.root),
                        patch.object(manager, 'CONFIG', self.root/'config.json'),
                        patch.object(manager, 'notification_ready', return_value=True),
                        patch.object(manager, 'running', return_value=True),
                        patch.object(manager, 'health', return_value={'latency_ms': 20}),
                        patch.object(telegram, 'notify')]
        isolation = [patch.object(manager.policy,'ROOT',self.root/'budget'),
                     patch.object(manager.policy,'CONFIG',self.root/'policy.json'),
                     patch.object(manager.policy,'operation',side_effect=lambda *a,**k:nullcontext()),
                     patch.object(manager,'start_notifications'), patch.object(manager,'stop_runtime'), patch.object(manager,'require_internet')]
        for item in isolation:
            item.start(); self.addCleanup(item.stop)
        self.mocks = [item.start() for item in self.patches]
        self.sender = self.mocks[-1]
        self.a = manager.new_record(parse_block('socks5://user:private-password@a.example:1080'))
        self.b = manager.new_record(parse_block('socks5://b.example:1080'))
        for record, title, delay in [(self.a,'日本-东京',30),(self.b,'新加坡',10)]:
            record.update(name=title,status='alive',reachability='reachable',latency_method='tcping',latency_checked_at=time.time(),failures=0,latency_ms=delay,ip='192.0.2.1',location_zh=title)
        self.data = dict(version=1,nodes=[self.a,self.b],auto=True,active_id=self.a['id'],delete_after=3)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def activate(self, data, record, **kwargs):
        data['active_id'] = record['id']
        record.update(status='alive',failures=0)
        return True

    def test_snapshot_contains_no_credentials_and_is_group_readable(self):
        manager.save(self.data)
        raw = (self.root/'node-summary.json').read_text()
        for secret in ['private-password','a.example','username','private_key']:
            self.assertNotIn(secret,raw)
        self.assertEqual((self.root/'node-store.json').stat().st_mode & 0o777,0o600)
        self.assertEqual((self.root/'node-summary.json').stat().st_mode & 0o777,0o640)
        self.assertIn('日本-东京', '\n'.join(summary_lines(json.loads(raw))))

    def test_selected_node_notification_uses_committed_node_and_failover_mode(self):
        with patch.object(manager,'activate',side_effect=self.activate):
            self.assertTrue(manager.manual_connect(self.data,self.b['id']))
        kw = self.sender.call_args.kwargs
        self.assertTrue(kw['snapshot']['auto'])
        self.assertEqual(kw['snapshot']['active_id'],self.b['id'])
        self.assertEqual(kw['events'][0]['previous'],'日本-东京')
        self.assertEqual(kw['events'][0]['current'],'新加坡')
        self.assertEqual(json.loads(manager.DATABASE.read_text())['active_id'],self.b['id'])

    def test_failover_reports_reason_previous_and_current(self):
        self.a['watch_failures']=1
        with patch.object(manager,'health',side_effect=RuntimeError('offline')),patch.object(manager,'activate',side_effect=self.activate):
            manager.watch(self.data)
        event = next(e for e in self.sender.call_args.kwargs['events'] if e['kind']=='failover')
        self.assertEqual((event['previous'],event['current']),('日本-东京','新加坡'))
        self.assertIn('连接失败',event['reason'])

    def test_failed_candidate_is_in_successful_fallback_event(self):
        events=[]
        with patch.object(manager,'activate',side_effect=[False,True]):
            manager.connect_next(self.data,events=events)
        self.assertEqual(events[0]['failed'],['日本-东京'])


    def test_send_failure_retained_and_retried_without_connection_rollback(self):
        self.sender.side_effect=ValueError('simulate failed API')
        with patch.object(manager,'activate',side_effect=self.activate):
            self.assertTrue(manager.manual_connect(self.data,self.b['id']))
        self.assertEqual(self.data['active_id'],self.b['id'])
        self.assertEqual(len(self.data['pending_notifications']),1)
        self.sender.side_effect=None
        manager.emit_events(self.data,[])
        self.assertEqual(self.data['pending_notifications'],[])
        self.assertEqual(self.sender.call_count,2)



class TelegramFormatting(unittest.TestCase):
    def test_unicode_pagination_preserves_text(self):
        text='日本节点😀失败\n'*2000
        chunks=split_message(text)
        self.assertEqual(''.join(chunks),text)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(chunk.encode('utf-16-le'))//2<=3500 for chunk in chunks))

    def test_event_uses_cached_ip_and_sends_all_long_list_parts(self):
        data={'nodes':[{'id':str(i),'name':'集成测试-日本东京😀'*4,'node':{'protocol':'wg'},
                        'status':'alive','failures':0,'latency_ms':5,'ip':'192.0.2.1','location_zh':'日本-东京'}
                       for i in range(80)],'active_id':'0','auto':True}
        snapshot=public_snapshot(data)
        with patch.object(telegram,'configured',return_value=True), \
             patch.object(telegram,'load_config',return_value={'token':'test','chat_id':'123'}), \
             patch.object(telegram,'service_snapshot',return_value=('active','test','192.0.2.2','WireGuard','开启','分流')), \
             patch.object(telegram,'proxy_info') as probe, \
             patch.object(telegram,'bot_api',return_value={'message_id':1}) as api, \
             patch.object(telegram.time,'sleep'):
            telegram.notify(snapshot=snapshot,events=[{'kind':'connected','current':'日本-东京'}])
        probe.assert_not_called()
        self.assertGreater(api.call_count,1)
        parts=[call.args[2]['text'] for call in api.call_args_list]
        self.assertTrue(all(len(text.encode('utf-16-le'))//2<=4096 for text in parts))
        text=''.join(parts)
        self.assertIn('代理出口 IP（最近检测）：192.0.2.1',text)
        self.assertIn('80.',text)


if __name__=='__main__':
    unittest.main()
