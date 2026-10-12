#!/usr/bin/env python3
"""Shared probe budgets, cooldowns and short-lived results. No proxy credentials."""
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

CONFIG = Path('/etc/sing-box/manager/probe-policy.json')
ROOT = Path('/var/lib/singbox-manager-budget')
DEFAULTS = {'connect_gap': 15, 'request_gap': 2, 'hourly_budget': 180,
            'cache_ttl': 120, 'provider_cooldown': 3600,
            'active_interval': 180}

class Deferred(RuntimeError):
    def __init__(self, message, retry_at=None, category='budget'):
        super().__init__(message)
        self.retry_at = retry_at or time.time() + 60
        self.category = category

class Restricted(Deferred):
    pass


def settings():
    data = dict(DEFAULTS)
    if CONFIG.exists():
        data.update({k:v for k,v in json.loads(CONFIG.read_text()).items() if k in DEFAULTS})
    for key in DEFAULTS:
        if not isinstance(data[key], int) or not 1 <= data[key] <= 604800:
            raise ValueError('检测策略数值无效：' + key)
    return data


def node_key(node):
    from node_formats import normalize
    return hashlib.sha256(json.dumps(normalize(node),sort_keys=True).encode()).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.policy-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o660)
            if os.geteuid()==0:
                try:os.fchown(f.fileno(),0,grp.getgrnam('sing-box').gr_gid)
                except KeyError:pass
            json.dump(value,f,ensure_ascii=False)
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


@contextmanager
def state_file(filename):
    ROOT.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(ROOT/(filename+'.lock')),os.O_RDWR|os.O_CREAT,0o660)
    try:
        if os.geteuid()==0:
            os.fchmod(fd,0o660)
            try:os.fchown(fd,0,grp.getgrnam('sing-box').gr_gid)
            except KeyError:pass
        fcntl.flock(fd,fcntl.LOCK_EX)
        path=ROOT/filename
        data=json.loads(path.read_text()) if path.exists() else {}
        yield data
        atomic(path,data)
    finally:os.close(fd)


def transact(state,request,config,now):
    """Per-server budget engine. No cross-server coordination."""
    op=request.get('op')
    if op=='release':
        if state.get('lease',{}).get('token')==request.get('token'):state.pop('lease',None)
        return {'ok':True}
    if op=='cooldown':
        until=now+min(86400,max(config['provider_cooldown'],int(request.get('seconds',0))))
        state['cooldown_until']=max(state.get('cooldown_until',0),until)
        state['cooldown_reason']=str(request.get('reason','上游拒绝检测'))[:120]
        return {'ok':True,'retry_at':state['cooldown_until']}
    if op=='status':
        return {k:state.get(k) for k in ('cooldown_until','cooldown_reason','last_new_session','deferred_until')}
    if op!='claim' or request.get('kind') not in ('current','connect','metadata'):
        raise ValueError('无效的检测额度请求')
    kind=request['kind']
    wait=[]
    if state.get('cooldown_until',0)>now:wait.append((state['cooldown_until'],'本机处于供应商限流冷却期'))
    if state.get('lease',{}).get('expires',0)>now:wait.append((state['lease']['expires'],'另一个检测正在进行'))
    if state.get('last_request',0)+config['request_gap']>now:wait.append((state['last_request']+config['request_gap'],'检测请求间隔限制'))
    history=[t for t in state.get('history',[]) if now-t<3600]
    state['history']=history
    # Each slot permits at most two health URLs, or up to three IP providers.
    cost=8 if kind=='connect' else 3 if kind=='metadata' else 2
    if len(history)+cost>config['hourly_budget']:
        wait.append(((history[min(len(history)-1,cost-1)] if history else now)+3600,'本机每小时检测预算已用完'))
    if kind == 'connect':
        gap=config['connect_gap']
        if state.get('last_new_session',0)+gap>now:wait.append((state['last_new_session']+gap,'新建检测会话间隔限制'))
    if wait:
        until,reason=max(wait)
        state['deferred_until']=until
        return {'ok':False,'retry_at':until,'reason':reason}
    state.pop('deferred_until',None)
    token=secrets.token_hex(16)
    state['history'].extend([now]*cost)
    state['last_request']=now
    if kind == 'connect':state['last_new_session']=now
    state['lease']={'token':token,'expires':now+240}
    return {'ok':True,'token':token}


def rpc(request):
    try:
        config=settings()
        with state_file('budget.json') as state:
            return transact(state,request,config,time.time())
    except (OSError, ValueError, TypeError):
        raise Deferred('本机检测策略或状态暂不可用；保留节点，稍后重试') from None


@contextmanager
def operation(kind, wait=False):
    result=rpc({'op':'claim','kind':kind})
    if wait and not result.get('ok') and 0 < result.get('retry_at',0)-time.time() <= min(60,settings()['connect_gap']):
        delay=max(0,result['retry_at']-time.time())
        print(f'  [本机频率限制] 等待 {delay:.0f} 秒再尝试…',flush=True)
        time.sleep(min(delay+0.05, 60))
        result=rpc({'op':'claim','kind':kind})
    if not result.get('ok'):
        raise Deferred(result.get('reason','检测已延后'),result.get('retry_at'))
    try:
        yield
    except Restricted as exc:
        try:rpc({'op':'cooldown','seconds':max(0,int(exc.retry_at-time.time())),'reason':str(exc)})
        except (Deferred,OSError):pass
        raise
    finally:
        try:rpc({'op':'release','token':result['token']})
        except (Deferred,OSError):pass


def cache_get(key,max_age=None):
    with state_file('cache.json') as data:
        item=data.get('nodes',{}).get(key,{})
        age=time.time()-item.get('health_at',0)
        if 0<=age<=(max_age if max_age is not None else settings()['cache_ttl']):return dict(item)
    return None


def cache_put(key,info,health=True,active=False):
    with state_file('cache.json') as data:
        nodes=data.setdefault('nodes',{});item=nodes.setdefault(key,{})
        item.update({k:v for k,v in info.items() if k in ('ip','city','region','country','country_code','org','source','location_zh','latency_ms','latency_url')})
        if health:item['health_at']=time.time()
        if info.get('ip'):item['ip_at']=time.time()
        if active:data['active_key']=key
        if len(nodes)>2000:
            for identity in sorted(nodes,key=lambda k:nodes[k].get('health_at',0))[:len(nodes)-2000]:
                if identity!=data.get('active_key'):nodes.pop(identity,None)


def cache_forget(key):
    with state_file('cache.json') as data:data.get('nodes',{}).pop(key,None)


def active_info():
    with state_file('cache.json') as data:
        return data.get('active_key'),dict(data.get('nodes',{}).get(data.get('active_key'),{}))


def init():
    ROOT.mkdir(parents=True,exist_ok=True)
    ROOT.chmod(0o2770)
    if os.geteuid()==0:os.chown(ROOT,0,grp.getgrnam('sing-box').gr_gid)
    atomic(CONFIG, settings())
    CONFIG.chmod(0o640)
    (ROOT/'transport.json').unlink(missing_ok=True)
    (ROOT/'transport.json.lock').unlink(missing_ok=True)
    with state_file('budget.json'):pass
    with state_file('cache.json'):pass


def main():
    if sys.argv[1:]==['init']:init();return
    if sys.argv[1:]==['status']:
        s=rpc({'op':'status'})
        print(json.dumps({'mode':'local',**s},ensure_ascii=False));return
    if len(sys.argv)>=7 and sys.argv[1]=='activate':
        protocol,mode,traffic,node_file,notify=sys.argv[2:7]
        key=node_key(json.loads(Path(node_file).read_text()))
        with operation('connect',wait=True):
            env=dict(os.environ,SBM_POLICY_MANAGED='1',SBM_NODE_KEY=key)
            result=subprocess.run(['bash',str(Path(__file__).with_name('runtime.sh')),'--apply-profile',protocol,mode,traffic,node_file,notify],env=env)
            # probe.py already records upstream restrictions in the shared budget.
            raise SystemExit(result.returncode)
    raise ValueError('用法：probe_policy.py init|status|activate ...')

if __name__=='__main__':
    from utf8_runtime import ensure_utf8
    ensure_utf8()
    try:main()
    except Deferred as exc:
        print('检测延后：'+str(exc),file=sys.stderr);raise SystemExit(75)
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        print('检测策略错误：'+type(exc).__name__,file=sys.stderr);raise SystemExit(75)
