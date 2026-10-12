import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'singbox-manager'))
import node_manager as m
from node_formats import parse_block
from node_ui import render_inventory, cell_width

class CoordinatedState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(m, key, self.root/name) for key,name in [('DATABASE','db.json'),('PROFILES','profiles'),('STATE','state'),('LOCK','lock')]]
        self.patches += [patch.object(m,'emit_events'), patch.object(m,'running',return_value=True)]
        self.mocks=[p.start() for p in self.patches]
        self.events=self.mocks[-2]
        m.PROFILES.mkdir();m.STATE.mkdir()
        self.a=m.new_record(parse_block('socks5://a.example:1080'))
        self.b=m.new_record(parse_block('socks5://b.example:1080'))
        for n in (self.a,self.b):n.update(status='alive',latency_ms=10,location_zh='日本-东京',ip='192.0.2.1')
        self.data=dict(version=1,nodes=[self.a,self.b],active_id=self.a['id'],auto=True,delete_after=3)
        m.write_json(m.PROFILES/'socks5.json',self.a['node']);m.save(self.data)
        (m.STATE/'active').write_text('socks5')

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def edit(self):
        edited=dict(self.a['node'],server='edited.example')
        m.save_node_edit(self.data,self.a['id'],edited)
        return edited

    def test_active_edit_is_draft_and_failed_apply_preserves_connection_and_auto(self):
        old=copy.deepcopy(self.a['node']); profile=(m.PROFILES/'socks5.json').read_bytes()
        edited=self.edit()
        self.assertEqual(self.a['node'],old)
        self.assertEqual(self.a['pending_node'],edited)
        self.assertEqual((m.PROFILES/'socks5.json').read_bytes(),profile)
        with patch.object(m.subprocess,'run') as run:
            run.return_value.returncode=1
            self.assertFalse(m.manual_connect(self.data,self.a['id']))
        self.assertEqual(self.a['status'],'alive');self.assertTrue(self.data['auto'])
        self.assertEqual(self.data['active_id'],self.a['id'])
        self.assertEqual(self.a['node'],old)
        self.assertIn('pending_error',self.a)

    def test_successful_apply_commits_draft_once_and_enables_failover(self):
        edited=self.edit()
        with patch.object(m.subprocess,'run') as run,patch.object(m,'refresh_active'):
            run.return_value.returncode=0
            self.assertTrue(m.manual_connect(self.data,self.a['id']))
        self.assertNotIn('pending_node',self.a); self.assertTrue(self.data['auto'])
        self.assertEqual(self.a['node'],edited)
        self.assertEqual(json.loads((m.PROFILES/'socks5.json').read_text()),edited)
        self.assertEqual(self.events.call_args.args[1][0]['kind'],'connected')

    def test_auto_connect_never_applies_pending_edits(self):
        self.edit()
        with patch.object(m,'activate',return_value=True) as activate:
            m.connect_best(self.data)
            self.assertFalse(activate.call_args.kwargs['apply_pending'])

    def test_inactive_edit_cannot_replace_active_protocol_file(self):
        before=(m.PROFILES/'socks5.json').read_bytes()
        m.save_node_edit(self.data,self.b['id'],dict(self.b['node'],server='new.example'))
        self.assertEqual((m.PROFILES/'socks5.json').read_bytes(),before)
        self.assertEqual(self.b['status'],'unknown')

    def test_duplicates_rejected_against_committed_and_pending_nodes(self):
        with self.assertRaises(ValueError):m.save_node_edit(self.data,self.b['id'],self.a['node'])
        edited=self.edit()
        with self.assertRaises(ValueError):m.save_node_edit(self.data,self.b['id'],edited)

    def test_protocol_picker_filters_deduplicates_and_cancel_does_not_mutate(self):
        self.data['nodes'].append(m.new_record(parse_block('https://u:p@proxy.example:443')))
        m.save(self.data)
        self.assertEqual(len(m.protocol_options(self.data,'socks5')),2)
        self.assertEqual(len(m.protocol_options(self.data,'https')),1)
        before=m.DATABASE.read_bytes()
        with patch('builtins.input',return_value='0'),redirect_stdout(io.StringIO()):
            self.assertFalse(m.connect_protocol('socks5'))
        self.assertEqual(before,m.DATABASE.read_bytes())

    def test_main_menu_switch_sets_same_inventory_active_id_and_event(self):
        with patch.object(m,'choose_protocol',return_value=self.b),patch.object(m.subprocess,'run') as run,patch.object(m,'refresh_active'):
            run.return_value.returncode=0
            self.assertTrue(m.connect_protocol('socks5'))
        fresh=m.load()
        self.assertEqual(fresh['active_id'],self.b['id']);self.assertTrue(fresh['auto'])
        self.assertIn('主菜单切换',self.events.call_args.args[1][0]['reason'])

    def test_mode_switch_preserves_auto_and_pending_edit(self):
        self.edit()
        with patch.object(m,'refresh_active'):
            m.mode_event(self.data,True,'切换 TUN')
        self.assertTrue(self.data['auto']);self.assertIn('pending_node',self.a)
        self.assertEqual(self.events.call_args.args[1][0]['kind'],'mode_changed')

    def test_stop_notifies_after_commit_before_disabling_monitor(self):
        seen=[]
        self.events.side_effect=lambda data,events:seen.append((data['active_id'],data['auto'],events[0]['kind']))
        with patch.object(m,'stop_runtime',side_effect=lambda:seen.append('stop')),patch.object(m.subprocess,'run',side_effect=lambda *a,**k:seen.append('timer')):
            m.manual_stop(self.data)
        self.assertEqual(seen,['stop',(None,False,'stopped'),'timer'])

    def test_failed_first_selection_automatically_tries_inventory(self):
        self.data.update(active_id=None,auto=False)
        def recover(data):
            self.assertTrue(data['auto']);data['active_id']=self.b['id']
        with patch.object(m,'activate',return_value=False),patch.object(m,'watch',side_effect=recover) as watch:
            self.assertTrue(m.manual_connect(self.data,self.a['id']))
        watch.assert_called_once();self.assertEqual(self.data['active_id'],self.b['id'])

    def test_watch_keeps_healthy_current_node_even_with_faster_spare(self):
        self.b['latency_ms']=1
        with patch.object(m,'health',return_value={'latency_ms':100}),patch.object(m,'check_record') as check,patch.object(m,'activate') as activate:
            m.watch(self.data)
        check.assert_not_called();activate.assert_not_called()
        self.assertEqual(self.data['active_id'],self.a['id'])

    def test_watch_retests_spares_sorts_latency_and_skips_failed_activation(self):
        c=m.new_record(parse_block('socks5://c.example:1080'));c.update(status='alive',latency_ms=1,failures=2)
        self.data['nodes'].append(c);self.a['failures']=2
        def check(record,**kw):
            self.assertFalse(kw['count_failure'])
            record.update(status='alive',latency_ms=2 if record is self.b else 20)
        attempted=[]
        def activate(data,record,**kw):
            attempted.append(record['id']);self.assertFalse(kw['apply_pending'])
            if record is self.b:return False
            data['active_id']=record['id'];return True
        with patch.object(m,'health',side_effect=RuntimeError('offline')),patch.object(m,'check_record',side_effect=check),patch.object(m,'activate',side_effect=activate):
            m.watch(self.data)
        self.assertEqual(attempted,[self.b['id'],c['id']])
        self.assertEqual(self.data['active_id'],c['id'])
        self.assertEqual(self.a['failures'],2);self.assertEqual(len(self.data['nodes']),3)
        self.assertEqual(self.events.call_args.args[1][0]['kind'],'failover')

    def test_watch_skips_when_user_disabled_auto(self):
        self.data['auto']=False
        with patch.object(m,'health') as health,patch.object(m,'activate') as activate:m.watch(self.data)
        health.assert_not_called();activate.assert_not_called()

    def test_reimport_old_config_after_edit_gets_unique_stable_id(self):
        old=dict(self.b['node'])
        m.save_node_edit(self.data,self.b['id'],dict(old,server='different.example'))
        record=m.unique_record(self.data,old)
        self.assertNotEqual(record['id'],self.b['id'])
        self.assertEqual(record['fingerprint'],m.fingerprint(old))

    def test_inventory_width_pagination_no_secrets(self):
        nodes=[dict(self.a,id=str(i),name='中国香港-长名称😀'*8) for i in range(10)]
        nodes[8]['pending_node']={'password':'secret-value'}
        view=render_inventory(dict(self.data,nodes=nodes),page=1,width=40,color=False)
        self.assertIn('09 ',view);self.assertNotIn('01 中国',view)
        self.assertIn('2/2',view);self.assertIn('待应用',view)
        self.assertNotIn('secret-value',view);self.assertNotIn('\x1b',view)
        self.assertTrue(all(cell_width(line)<=40 for line in view.splitlines()))

if __name__=='__main__':unittest.main()
