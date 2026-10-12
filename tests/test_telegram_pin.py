import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'singbox-manager'))
import telegram_monitor as tg

class AlertPin(unittest.TestCase):
    def send(self,events=None,active='active',snapshot=None,fail_pin=False,parts=1):
        calls=[]
        def api(token,method,payload):
            calls.append((method,payload))
            if method=='pinChatMessage':
                if fail_pin:raise ValueError('Bad Request: not enough rights')
                return True
            return {'message_id':100+sum(m=='sendMessage' for m,_ in calls)}
        with patch.object(tg,'load_config',return_value={'token':'unit-test-token','chat_id':'123'}),patch.object(tg,'configured',return_value=True),patch.object(tg,'service_snapshot',return_value=(active,'1.14','192.0.2.1','WireGuard','开启','分流')),patch.object(tg,'load_summary',return_value={}),patch.object(tg,'proxy_info',return_value={'ip':'192.0.2.2'}),patch.object(tg,'split_message',return_value=['part']*parts),patch.object(tg.time,'sleep'),patch.object(tg,'bot_api',side_effect=api),redirect_stdout(io.StringIO()) as log:
            result=tg.notify(events=events,snapshot=snapshot)
        return result,calls,log.getvalue()

    def test_current_failure_and_failover_are_pinned(self):
        for kind in ('failover','connect_failed','direct_fallback','direct_restore_failed','probe_deferred'):
            result,calls,_=self.send(events=[{'kind':kind}])
            self.assertEqual([method for method,_ in calls],['sendMessage','pinChatMessage'])
            self.assertEqual(calls[-1][1]['message_id'],result[0])
            self.assertEqual(calls[-1][1]['chat_id'],'123')

    def test_regular_healthy_and_manual_stop_are_not_pinned(self):
        for events,active in ((None,'active'),([{'kind':'connected'}],'active'),([{'kind':'stopped'}],'inactive'),([{'kind':'deleted','reason':'用户手动删除'}],'active')):
            _,calls,_=self.send(events=events,active=active)
            self.assertEqual([method for method,_ in calls],['sendMessage'])

    def test_periodic_service_outage_is_pinned(self):
        _,calls,_=self.send(active='inactive')
        self.assertEqual(calls[-1][0],'pinChatMessage')

    def test_paginated_alert_pins_first_part_and_preserves_other_pins(self):
        ids,calls,_=self.send(events=[{'kind':'direct_fallback'}],parts=3)
        self.assertEqual(ids,[101,102,103])
        self.assertEqual([method for method,_ in calls],['sendMessage']*3+['pinChatMessage'])
        self.assertEqual(calls[-1][1]['message_id'],101)

    def test_pin_denied_keeps_successful_delivery_without_resending(self):
        ids,calls,log=self.send(events=[{'kind':'connect_failed'}],fail_pin=True)
        self.assertEqual(ids,[101]);self.assertIn('置顶失败',log);self.assertIn('not enough rights',log)
        self.assertEqual([method for method,_ in calls],['sendMessage','pinChatMessage'])

    def test_pin_error_redacts_token_and_timeout_is_nonfatal(self):
        for exc in (ValueError('unit-test-token denied'),TimeoutError('timeout')):
            with patch.object(tg,'bot_api',side_effect=exc),redirect_stdout(io.StringIO()) as log:
                self.assertFalse(tg.pin_alert({'token':'unit-test-token','chat_id':'123'},9))
            self.assertNotIn('unit-test-token',log.getvalue())

if __name__=='__main__':unittest.main()
