import copy
from contextlib import nullcontext
import errno
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'singbox-manager'))
import transport_probe as transport
import probe_policy as policy
import node_manager as manager
import node_health
from node_formats import parse_block
from test_nodes import WG

class Transport(unittest.TestCase):
    def test_tcp_probe_sends_no_tls_auth_or_application_data(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
            port=listener.getsockname()[1];received=[]
            def accept():
                conn,_=listener.accept()
                with conn:
                    conn.settimeout(2);received.append(conn.recv(1024))
            worker=threading.Thread(target=accept);worker.start()
            with patch.object(transport,'direct_socket',side_effect=lambda family,kind:socket.socket(family,kind)):
                info=transport.measure(parse_block(f'socks5://user:secret@127.0.0.1:{port}'))
            worker.join(4)
        self.assertEqual(received,[b'']);self.assertEqual(info['reachability'],'reachable')
        self.assertEqual(info['latency_method'],'tcping');self.assertGreaterEqual(info['latency_ms'],0)

    def test_wireguard_uses_marked_ping_only(self):
        node=parse_block(WG)
        with patch.object(transport,'interface',return_value='eth0'),patch.object(transport,'direct_mark',return_value=0x2024),patch.object(transport.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'64 bytes: time=12.5 ms','')) as run,patch.object(transport,'direct_socket') as sock:
            info=transport.measure(node)
        command=run.call_args.args[0]
        self.assertIn('ping',command);self.assertIn(str(0x2024),command);self.assertIn('-6',command)
        sock.assert_not_called();self.assertEqual(info['latency_ms'],12.5)

    def test_icmp_timeout_is_unknown_but_tcp_refusal_is_unreachable(self):
        with patch.object(transport,'interface',return_value='eth0'),patch.object(transport.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','')):
            self.assertEqual(transport.measure(parse_block(WG))['reachability'],'unknown')
        sock=unittest.mock.MagicMock();sock.__enter__.return_value=sock;sock.connect.side_effect=ConnectionRefusedError(errno.ECONNREFUSED,'refused')
        with patch.object(transport,'direct_socket',return_value=sock):
            self.assertEqual(transport.measure(parse_block('socks5://192.0.2.1:1080'))['reachability'],'unreachable')

    def test_local_routing_failure_does_not_mark_remote_dead(self):
        with patch.object(transport,'direct_socket',side_effect=PermissionError('no mark permission')):
            info=transport.measure(parse_block('https://u:p@192.0.2.1:443'))
        self.assertEqual(info['reachability'],'unknown')

    def test_marks_bypass_both_tun_and_tproxy(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(transport,'CONFIG',Path(directory)/'config.json'):
            transport.CONFIG.write_text('{"inbounds":[{"type":"tun","auto_redirect":true}]}')
            self.assertEqual(transport.direct_mark(),0x2024)
            transport.CONFIG.write_text('{"inbounds":[{"type":"tproxy"}]}')
            self.assertEqual(transport.direct_mark(),0x4002)

class Budget(unittest.TestCase):
    def test_connect_gap_budget_and_release_are_local(self):
        state={};config=dict(policy.DEFAULTS);now=10000
        first=policy.transact(state,{'op':'claim','kind':'connect'},config,now)
        self.assertTrue(first['ok'])
        policy.transact(state,{'op':'release','token':first['token']},config,now+1)
        refused=policy.transact(state,{'op':'claim','kind':'connect'},config,now+2)
        self.assertFalse(refused['ok']);self.assertEqual(refused['retry_at'],now+15)
        self.assertTrue(policy.transact(state,{'op':'claim','kind':'connect'},config,now+16)['ok'])

    def test_only_provider_429_pauses_authentication_not_wrong_password(self):
        from probe_errors import upstream_restriction
        self.assertIsNone(upstream_restriction('outbound/http: authentication required 407'))
        self.assertIsInstance(upstream_restriction('outbound/http: unexpected status: 429'),policy.Restricted)
        self.assertIsNone(upstream_restriction('inbound/socks: unexpected status: 429'))

    def test_query_site_429_is_not_a_dead_proxy(self):
        with patch.object(node_health,'request',return_value=('',429,10)):
            with self.assertRaises(policy.Deferred):node_health.health(18081)

class Inventory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for module,key,value in [(manager,'PROFILES',self.root/'profiles'),(manager,'DATABASE',self.root/'db.json'),
                                 (manager,'STATE',self.root/'state'),(manager,'CONFIG',self.root/'config.json'),
                                 (policy,'ROOT',self.root/'budget'),(policy,'CONFIG',self.root/'policy.json')]:
            p=patch.object(module,key,value);p.start();self.addCleanup(p.stop)
        for key in ('emit_events','stop_runtime'):
            p=patch.object(manager,key);p.start();self.addCleanup(p.stop)
        self.a=manager.new_record(parse_block('socks5://a.example:1080'))
        self.b=manager.new_record(parse_block('socks5://b.example:1080'))
        for item,ms in ((self.a,3),(self.b,20)):
            item.update(reachability='reachable',latency_ms=ms,latency_method='tcping',latency_checked_at=time.time())
        self.data=dict(version=1,latency_schema='transport-v1',nodes=[self.a,self.b],active_id=None,auto=True)

    def test_tcp_fastest_actual_failure_is_deleted_then_next_connects(self):
        self.a['http_latency_ms']=999;self.b['http_latency_ms']=1
        def refreshed(record):record['status']='alive'
        with patch.object(manager.subprocess,'run',side_effect=[subprocess.CompletedProcess([],1),subprocess.CompletedProcess([],0)]) as run,patch.object(manager,'refresh_active',side_effect=refreshed):
            self.assertTrue(manager.connect_best(self.data))
        self.assertEqual(self.data['nodes'],[self.b]);self.assertEqual(self.data['active_id'],self.b['id'])
        self.assertEqual(run.call_count,2)

    def test_deferred_actual_verification_keeps_all_nodes_and_stops_attempts(self):
        with patch.object(manager.subprocess,'run',return_value=subprocess.CompletedProcess([],75)) as run:
            self.assertFalse(manager.connect_best(self.data))
        self.assertEqual(len(self.data['nodes']),2);self.assertEqual(run.call_count,1)

    def test_scattered_monitor_tests_one_due_backup_per_interval(self):
        self.a['latency_checked_at']=0;self.b['latency_checked_at']=0
        with patch.object(manager,'test_record',return_value={'reachability':'reachable','latency_ms':5}) as measured:
            manager.scan(self.data,scheduled=True)
            manager.scan(self.data,scheduled=True)
            self.assertEqual(measured.call_count,1)
            self.data['last_standby_check']=0
            manager.scan(self.data,scheduled=True)
            self.assertEqual(measured.call_count,2)
            self.assertEqual(measured.call_args.args[0]['id'],self.b['id'])

    def test_repeated_manual_scans_use_cache_and_never_authenticate(self):
        with patch.object(manager,'test_record') as measured,patch.object(manager,'activate') as activated,patch.object(manager,'health') as health:
            for _ in range(3):manager.scan(self.data)
        measured.assert_not_called();activated.assert_not_called();health.assert_not_called()

    def test_shortened_standby_interval_is_not_blocked_by_longer_cache(self):
        self.data['nodes']=[self.a];self.a['latency_checked_at']=time.time()-70
        with patch.object(policy,'settings',return_value=dict(policy.DEFAULTS,standby_interval=60)),patch.object(manager,'test_record',return_value={'reachability':'reachable','latency_ms':8}) as measure:
            manager.scan(self.data,scheduled=True)
        measure.assert_called_once()

    def test_deferred_transport_probe_never_uses_stale_score_to_connect(self):
        for r in self.data['nodes']:r['latency_checked_at']=0
        with patch.object(manager,'test_record',side_effect=policy.Deferred('budget')),patch.object(manager,'activate') as activate:
            self.assertFalse(manager.connect_best(self.data))
        activate.assert_not_called();self.assertEqual(len(self.data['nodes']),2)

    def test_silent_wireguard_never_deleted_by_transport_checks(self):
        node=manager.new_record(parse_block(WG));self.data['nodes']=[node]
        with patch.object(manager,'test_record',return_value={'reachability':'unknown','latency_ms':None}):
            for _ in range(4):
                node['latency_checked_at']=0;self.data['last_standby_check']=0
                manager.scan(self.data,scheduled=True)
        self.assertEqual(self.data['nodes'],[node]);self.assertEqual(node['failures'],0)

    def test_local_cache_write_error_keeps_verified_active_connection(self):
        self.data['active_id']=self.a['id']
        with patch.object(manager,'running',return_value=True),patch.object(policy,'operation',side_effect=lambda *a,**k:nullcontext()),patch.object(manager,'health',return_value={'latency_ms':100}),patch.object(policy,'cache_put',side_effect=OSError('disk')),patch.object(manager,'connect_best') as connect:
            manager.watch(self.data)
        self.assertIn(self.a,self.data['nodes']);self.assertEqual(self.a['status'],'alive')
        connect.assert_not_called()

    def test_proxy_confirmation_failure_deletes_without_round_threshold(self):
        self.data['active_id']=self.a['id']
        with patch.object(manager,'running',return_value=True),patch.object(policy,'operation',side_effect=lambda *a,**k:nullcontext()),patch.object(manager,'health',side_effect=RuntimeError('both HTTPS targets failed')),patch.object(manager,'connect_best',return_value=True) as connect:
            manager.watch(self.data)
        self.assertNotIn(self.a,self.data['nodes']);connect.assert_called_once()

    def test_refresh_http_latency_never_replaces_tcp_rank(self):
        manager.STATE.mkdir();(manager.STATE/'probe-result.json').write_text(json.dumps({'protocol':'socks5','checked_at':time.time(),'latency_ms':999,'ip':'192.0.2.1','country':'JP'}))
        manager.refresh_active(self.a)
        self.assertEqual(self.a['latency_ms'],3);self.assertEqual(self.a['http_latency_ms'],999)
        self.assertIn('日本',self.a['name'])

    def test_legacy_failed_proxy_status_requires_fresh_verification(self):
        self.a['status']='dead';manager.write_json(manager.DATABASE,self.data)
        result=manager.load()
        self.assertEqual(result['nodes'][0]['status'],'unknown')
        self.assertEqual(result['failure_policy'],'immediate-v1')

    def test_legacy_http_ranking_is_invalidated_on_upgrade(self):
        data=copy.deepcopy(self.data);data.pop('latency_schema');manager.write_json(manager.DATABASE,data)
        updated=manager.load()
        self.assertIsNone(updated['nodes'][0]['latency_ms'])
        self.assertEqual(updated['nodes'][0]['http_latency_ms'],3)

if __name__=='__main__':unittest.main()
