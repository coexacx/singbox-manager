import copy
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'singbox-manager'))
import node_manager as m
import direct_network
import restore_direct
import telegram_monitor as tg
from node_formats import parse_block
from node_notifications import public_snapshot

class Sequential(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.mocks={}
        for module,key,value in [(m,'DATABASE',self.root/'db'),(m,'PROFILES',self.root/'profiles'),(m,'STATE',self.root/'state'),(m,'CONFIG',self.root/'config'),(m.policy,'ROOT',self.root/'budget'),(m.policy,'CONFIG',self.root/'policy')]:
            p=patch.object(module,key,value);p.start();self.addCleanup(p.stop)
        for key in ('emit_events','stop_runtime','require_internet','start_notifications'):
            p=patch.object(m,key);self.mocks[key]=p.start();self.addCleanup(p.stop)
        for key,value in [('running',True),('health',{'latency_ms':20})]:
            p=patch.object(m,key,return_value=value);self.mocks[key]=p.start();self.addCleanup(p.stop)
        p=patch.object(m.policy,'operation',side_effect=lambda *a,**k:nullcontext());p.start();self.addCleanup(p.stop)
        self.nodes=[m.new_record(parse_block(f'socks5://{x}.example:1080')) for x in 'abc']
        for i,r in enumerate(self.nodes):r.update(latency_ms=100-i*40,reachability='unreachable')
        self.data=dict(version=1,nodes=list(self.nodes),auto=True,active_id=None)

    def calls(self,codes,fn):
        with patch.object(m.subprocess,'run',side_effect=[subprocess.CompletedProcess([],code) for code in codes]) as run:
            fn()
        return run

    def test_list_order_ignores_legacy_latency_and_stops_on_success(self):
        events=[]
        self.calls([1,0],lambda:self.assertTrue(m.connect_next(self.data,events=events)))
        self.assertEqual(self.data['nodes'],self.nodes[1:]);self.assertEqual(self.data['active_id'],self.nodes[1]['id'])
        self.assertEqual([e['kind'] for e in events],['deleted','auto_connected'])
        self.assertNotIn('proxy_checked_at',self.nodes[2])

    def test_selected_position_then_wrap(self):
        events=[]
        self.calls([1,1,0],lambda:self.assertTrue(m.connect_next(self.data,start_id=self.nodes[1]['id'],events=events)))
        self.assertEqual(self.data['nodes'],[self.nodes[0]])
        self.assertEqual(events[-1]['failed'],[m.name(self.nodes[1]),m.name(self.nodes[2])])

    def test_manual_failure_continues_from_selected_successor(self):
        self.calls([1,0],lambda:self.assertTrue(m.manual_connect(self.data,self.nodes[1]['id'])))
        self.assertEqual(self.data['active_id'],self.nodes[2]['id'])
        self.assertEqual(self.data['nodes'],[self.nodes[0],self.nodes[2]])

    def test_all_failed_stops_before_notification_then_stays_idle(self):
        order=[]
        self.mocks['stop_runtime'].side_effect=lambda:order.append('stop')
        self.mocks['emit_events'].side_effect=lambda data,events:order.append(('notify',data.get('direct_restored'),[e['kind'] for e in events]))
        self.calls([1,1,1],lambda:self.assertFalse(m.manual_connect(self.data,self.nodes[0]['id'])))
        self.assertEqual(self.data['nodes'],[]);self.assertFalse(self.data['auto']);self.assertTrue(self.data['exhausted'])
        self.assertEqual(order[0],'stop');self.assertTrue(order[-1][1]);self.assertIn('direct_fallback',order[-1][2])
        with patch.object(m,'activate') as activate:
            m.watch(self.data);m.watch(self.data)
        activate.assert_not_called();self.assertEqual(self.mocks['stop_runtime'].call_count,1)

    def test_cleanup_failure_is_not_reported_as_restored_and_retries_without_proxy(self):
        self.data['nodes']=[];events=[]
        self.mocks['stop_runtime'].side_effect=subprocess.CalledProcessError(1,['stop'])
        m.connect_next(self.data,events=events)
        self.assertFalse(self.data['direct_restored']);self.assertTrue(self.data['direct_restore_pending'])
        self.assertEqual(events[-1]['kind'],'direct_restore_failed')
        self.mocks['stop_runtime'].side_effect=None
        with patch.object(m,'activate') as activate:m.watch(self.data)
        activate.assert_not_called();self.assertTrue(self.data['direct_restored'])
        self.assertEqual(self.mocks['emit_events'].call_args.args[1][-1]['kind'],'direct_fallback')

    def test_deferred_preserves_cursor_nodes_and_no_shutdown(self):
        self.calls([75],lambda:self.assertFalse(m.connect_next(self.data,start_id=self.nodes[1]['id'])))
        self.assertEqual(self.data['next_id'],self.nodes[1]['id']);self.assertEqual(self.data['nodes'],self.nodes)
        self.assertNotIn('exhausted',self.data);self.mocks['stop_runtime'].assert_not_called()
        self.data['deferred_until']=0
        self.calls([0],lambda:self.assertTrue(m.connect_next(self.data)))
        self.assertEqual(self.data['active_id'],self.nodes[1]['id'])

    def test_current_failure_uses_successor_and_wraps_without_ranking(self):
        self.data['active_id']=self.nodes[2]['id']
        self.mocks['health'].side_effect=RuntimeError('both targets failed')
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')),patch.object(m,'connect_next',return_value=True) as connect:
            m.watch(self.data)
        self.assertEqual(connect.call_args.kwargs['start_id'],self.nodes[0]['id'])
        self.assertNotIn(self.nodes[2],self.data['nodes']);self.mocks['require_internet'].assert_called_once()

    def test_local_outage_preserves_every_node_and_service(self):
        self.data['active_id']=self.nodes[0]['id'];self.mocks['health'].side_effect=RuntimeError('offline')
        self.mocks['require_internet'].side_effect=m.policy.Deferred('local outage')
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')),patch.object(m,'activate') as activate:
            m.watch(self.data)
        self.assertEqual(self.data['nodes'],self.nodes);activate.assert_not_called();self.mocks['stop_runtime'].assert_not_called()

    def test_healthy_active_and_legacy_monitor_never_contact_standbys(self):
        self.data['active_id']=self.nodes[0]['id'];self.nodes[0]['proxy_checked_at']=time.time()
        with patch.object(m,'activate') as activate,patch.object(m.subprocess,'run'):
            m.scan(self.data,choose_best=True)
            for _ in range(3):m.scan(self.data,scheduled=True);m.watch(self.data)
        activate.assert_not_called();self.mocks['health'].assert_not_called()

    def test_active_interval_and_only_one_current_request(self):
        self.data['active_id']=self.nodes[0]['id'];self.nodes[0]['proxy_checked_at']=time.time()-181
        m.watch(self.data);m.watch(self.data)
        self.mocks['health'].assert_called_once();self.assertNotIn('proxy_checked_at',self.nodes[1])

    def test_failed_notification_retried_after_auto_suspended(self):
        self.data.update(nodes=[],auto=False,exhausted=True,direct_restored=True,pending_notifications=[{'kind':'direct_fallback'}])
        m.watch(self.data)
        self.mocks['emit_events'].assert_called_once_with(self.data,[]);self.mocks['health'].assert_not_called()

    def test_import_offline_keeps_unverified_nodes_even_after_exhaustion(self):
        path=self.root/'input';path.write_text('1\nsocks5://import.example:1080')
        self.data.update(nodes=[],auto=False,exhausted=True,direct_restored=True)
        with patch('socket.socket',side_effect=AssertionError('network forbidden')),patch.object(m,'activate') as activate:
            m.import_file(self.data,path)
        activate.assert_not_called();self.assertEqual(self.data['nodes'][0]['status'],'unknown');self.assertFalse(self.data['auto'])

    def test_migration_removes_transport_results_without_reordering_or_credentials_changes(self):
        m.write_json(m.DATABASE,self.data);updated=m.load()
        self.assertEqual([r['node'] for r in updated['nodes']],[r['node'] for r in self.nodes])
        self.assertEqual(updated['selection_policy'],'sequential-v1');self.assertNotIn('latency_ms',updated['nodes'][0])

    def test_pending_edit_failure_keeps_preexisting_auto_preference(self):
        self.data.update(active_id=self.nodes[0]['id'],auto=False)
        self.nodes[0]['pending_node']=dict(self.nodes[0]['node'],server='edited.example')
        self.calls([1],lambda:self.assertFalse(m.manual_connect(self.data,self.nodes[0]['id'])))
        self.assertFalse(self.data['auto']);self.assertEqual(self.data['active_id'],self.nodes[0]['id'])

class DirectRecovery(unittest.TestCase):
    def test_cleanup_preserves_unrelated_route_rules(self):
        for rule in ({'priority':1,'table':'3420794334'}, {'priority':9000,'fwmark':'0x42','goto':9002}, {'priority':32768,'table':'2022','fwmark':'0x42'}, {'priority':32766,'table':'main'}):
            self.assertFalse(restore_direct.owned_rule(rule))
        self.assertTrue(restore_direct.owned_rule({'priority':9000,'fwmark':'0x2024','goto':9002}))
        self.assertTrue(restore_direct.owned_rule({'priority':18082,'fwmark':'0x4001','table':'18082'}))

    def test_failed_local_network_is_deferred(self):
        with patch.object(direct_network,'resolve',side_effect=OSError('no network')):
            with self.assertRaises(m.policy.Deferred):direct_network.require_internet()

    def test_outage_notification_reports_direct_mode_and_host_ip_without_proxy_query(self):
        snapshot=public_snapshot(dict(nodes=[],auto=False,exhausted=True,direct_restored=True,availability_alert=True))
        with patch.object(tg,'pin_alert'),patch.object(tg,'load_config',return_value={'token':'test','chat_id':'1'}),patch.object(tg,'configured',return_value=True),patch.object(tg,'service_snapshot',return_value=('inactive','1.14','192.0.2.1','WireGuard','开启','全局')),patch.object(tg,'proxy_info') as proxy,patch.object(tg,'bot_api',return_value={'message_id':1}) as api:
            tg.notify(snapshot=snapshot,events=[{'kind':'direct_fallback'}])
        proxy.assert_not_called();text=api.call_args.args[2]['text']
        self.assertIn('已恢复直连',text);self.assertIn('本机 IP：192.0.2.1',text);self.assertIn('流量模式：直连',text)
        self.assertIn('TUN：关闭',text)

if __name__=='__main__':unittest.main()
