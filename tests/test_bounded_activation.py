import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'singbox-manager'))
import probe
import ip_lookup
from deadline import deadline

class ProbeBounds(unittest.TestCase):
    def test_healthy_proxy_survives_all_ip_services_unavailable(self):
        with patch('node_health.health',return_value={'latency_ms':12}),patch.object(probe,'lookup_proxy_ip',side_effect=RuntimeError('429')),redirect_stdout(io.StringIO()) as output:
            info=probe.test_exit_ip(18081,'wg')
        self.assertEqual(info['latency_ms'],12);self.assertNotIn('ip',info)
        self.assertIn('稍后监测会重试',output.getvalue())

    def test_broken_https_still_rejects_node(self):
        with patch('node_health.health',side_effect=RuntimeError('offline')),patch.object(probe,'lookup_proxy_ip') as lookup,redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):probe.test_exit_ip(18081,'socks5')
            lookup.assert_not_called()

    def test_ip_budget_is_shared_by_services_and_timeouts(self):
        now=[0.0];calls=[]
        def request(args,**kw):
            calls.append(kw['timeout']);now[0]+=kw['timeout']
            raise subprocess.TimeoutExpired(args,kw['timeout'])
        with patch.object(ip_lookup.time,'monotonic',side_effect=lambda:now[0]),patch.object(ip_lookup.subprocess,'run',side_effect=request):
            with self.assertRaises(RuntimeError):ip_lookup.lookup_proxy_ip(1,timeout=8)
        self.assertEqual(calls,[7,1]);self.assertEqual(now[0],8)

    def test_ip_429_falls_back_and_returns_valid_second_source(self):
        responses=[subprocess.CompletedProcess([],0,'{}\n429',''),subprocess.CompletedProcess([],0,'{"ip":"192.0.2.9","country":"Japan"}\n200','')]
        with patch.object(ip_lookup.subprocess,'run',side_effect=responses):
            result=ip_lookup.lookup_proxy_ip(1)
        self.assertEqual(result['ip'],'192.0.2.9');self.assertEqual(result['source'],'ipwho.is')

    def test_deadline_interrupts_blocking_operation_and_restores_handler(self):
        old=signal.getsignal(signal.SIGALRM);start=time.monotonic()
        with self.assertRaises(TimeoutError):
            with deadline(0.03):time.sleep(2)
        self.assertLess(time.monotonic()-start,0.5)
        self.assertEqual(signal.getsignal(signal.SIGALRM),old)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0],0)

class ShellTransaction(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for folder in ('lib/templates','profiles','state','bin','config'): (self.root/folder).mkdir(parents=True)
        (self.root/'lib/API_VERSION').write_text('5')
        (self.root/'lib/templates/socks5.json').write_text('{}')
        (self.root/'profiles/socks5.json').write_text('{}')
        (self.root/'state/installed').touch()
        for name,content in {'active':'https','tun_mode':'on','traffic_mode':'split','exit_ip':'192.0.2.1','probe-result.json':'{"ip":"192.0.2.1"}'}.items(): (self.root/'state'/name).write_text(content)
        (self.root/'config/config.json').write_text('old-config')
        (self.root/'service-active').write_text('1')
        (self.root/'service-enabled').write_text('1')
        (self.root/'lib/render.py').write_text('import sys;open(sys.argv[3],"w").write("new-config")')
        (self.root/'lib/probe.py').write_text('''import os,sys,pathlib
r=pathlib.Path(os.environ['TEST_ROOT'])
if sys.argv[1]=='--active':
 (r/'state/exit_ip').write_text('192.0.2.2')
 (r/'state/probe-result.json').write_text('{}')
 sys.exit(1 if (r/'fail-post').exists() else 0)
sys.exit(1 if (r/'fail-pre').exists() else 0)
''')
        self.command('systemctl','''#!/usr/bin/env python3
import os,sys,pathlib
r=pathlib.Path(os.environ['TEST_ROOT']);a=sys.argv[1]
with (r/'trace').open('a') as f:f.write(a+'\\n')
if a in ['is-active','is-enabled']:sys.exit(0 if (r/('service-active' if a=='is-active' else 'service-enabled')).read_text()=='1' else 3)
if a in ['start','restart','stop']:(r/'service-active').write_text('0' if a=='stop' else '1')
if a in ['enable','disable']:(r/'service-enabled').write_text('1' if a=='enable' else '0')
''')
        for name in ('sing-box','chown','resolvectl','nscd'):self.command(name,'#!/bin/sh\nexit 0\n')
        source=(Path(__file__).resolve().parents[1]/'singbox.sh').read_text().rsplit('main "$@"',1)[0]
        (self.root/'functions.sh').write_text(source)
        self.before={p.name:p.read_bytes() for p in (self.root/'state').iterdir()}

    def command(self,name,content):
        p=self.root/'bin'/name;p.write_text(content);p.chmod(0o755)

    def tearDown(self):self.tmp.cleanup()

    def run_switch(self):
        script='''source "$TEST_ROOT/functions.sh"
LIB=$TEST_ROOT/lib; STATE=$TEST_ROOT/state; PROFILES=$TEST_ROOT/profiles
CONFIG_DIR=$TEST_ROOT/config; CONFIG=$CONFIG_DIR/config.json; MARKER=$STATE/installed
start_telegram_monitor() { :; }
enable_profile socks5 off global
'''
        return subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=10,env=dict(os.environ,TEST_ROOT=str(self.root),PATH=str(self.root/'bin')+':'+os.environ['PATH']))

    def test_postcheck_failure_restores_config_modes_exit_metadata_and_service(self):
        (self.root/'fail-post').touch();result=self.run_switch()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'config/config.json').read_text(),'old-config')
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'state').iterdir()},self.before)
        self.assertEqual((self.root/'service-active').read_text(),'1')
        self.assertIn('已恢复先前配置',result.stderr)

    def test_precheck_failure_never_restarts_original_service(self):
        (self.root/'fail-pre').touch();result=self.run_switch()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'trace').exists())
        self.assertEqual((self.root/'config/config.json').read_text(),'old-config')

    def test_success_commits_and_cleans_private_backup(self):
        result=self.run_switch();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'config/config.json').read_text(),'new-config')
        self.assertEqual((self.root/'state/active').read_text().strip(),'socks5')
        self.assertEqual((self.root/'state/tun_mode').read_text().strip(),'off')
        self.assertFalse(list((self.root/'state').glob('.switch.*')))

if __name__=='__main__':unittest.main()
