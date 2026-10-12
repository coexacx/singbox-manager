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
import direct_network as transport
import probe_policy as policy
import node_manager as manager
import node_health
from node_formats import parse_block
from test_nodes import WG

class Transport(unittest.TestCase):




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


    def test_local_detection_tool_error_is_not_a_dead_proxy(self):
        with patch.object(node_health,'request',side_effect=FileNotFoundError('curl')):
            with self.assertRaises(policy.Deferred):node_health.health(18081)

    def test_deferred_deadline_is_visible_without_consuming_another_slot(self):
        state={};config=dict(policy.DEFAULTS)
        first=policy.transact(state,{'op':'claim','kind':'connect'},config,10000)
        policy.transact(state,{'op':'release','token':first['token']},config,10001)
        deferred=policy.transact(state,{'op':'claim','kind':'connect'},config,10002)
        status=policy.transact(state,{'op':'status'},config,10002)
        self.assertEqual(status['deferred_until'],deferred['retry_at'])

if __name__=='__main__':unittest.main()
