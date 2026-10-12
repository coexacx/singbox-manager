#!/usr/bin/env python3
"""Persistent node inventory, import, health monitoring and sticky failover."""

import argparse
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
import secrets
from pathlib import Path
import subprocess
import tempfile
import time

from node_formats import normalize, parse_numbered
from node_health import health, chinese_location
import probe_policy as policy
from transport_probe import measure
from probe_errors import upstream_restriction
from render import render
from node_notifications import public_snapshot
import telegram_monitor
from node_ui import PAGE_SIZE, render_inventory, render_protocol
from deadline import deadline

LIB = Path(__file__).resolve().parent
PROFILES = Path("/etc/sing-box/manager")
STATE = Path("/var/lib/singbox-manager")
DATABASE = PROFILES / "node-store.json"
IMPORT_FILE = PROFILES / "nodes.txt"
LOCK = Path("/run/lock/singbox-manager.lock")
CONFIG = Path("/etc/sing-box/config.json")
LABELS = {"wg": "WireGuard", "https": "HTTPS", "shadowsocks": "Shadowsocks", "socks5": "SOCKS5"}


def write_json(path, data, shared=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".nodes-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            if shared:
                try:
                    group = grp.getgrnam("sing-box").gr_gid
                except KeyError:
                    group = os.getgid()
                os.fchown(stream.fileno(), os.getuid(), group)
                os.fchmod(stream.fileno(), 0o640)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load():
    if not DATABASE.exists():
        return {"version": 1, "latency_schema": "transport-v1", "nodes": [], "auto": False, "active_id": None}
    data = json.loads(DATABASE.read_text())
    if data.get("version") != 1 or not isinstance(data.get("nodes"), list):
        raise ValueError("节点数据库格式无效，请检查备份")
    if data.get('latency_schema') != 'transport-v1':
        for record in data['nodes']:
            record['http_latency_ms'] = record.get('latency_ms')
            record.update(latency_ms=None, latency_method=None, latency_checked_at=None, reachability='unknown', failures=0)
        data['latency_schema'] = 'transport-v1'
    return data


def save(data, publish=True):
    write_json(DATABASE, data)
    if publish:
        write_json(PROFILES / "node-summary.json", public_snapshot(data), shared=True)


def notification_ready():
    try:
        return telegram_monitor.configured() and subprocess.run(
            ["systemctl", "is-enabled", "--quiet", "singbox-manager-telegram.timer"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def emit_events(data, events):
    """Commit first; retry undelivered events on the next scan/operation."""
    if not notification_ready():
        return
    pending = data.setdefault("pending_notifications", [])
    pending.extend(dict(event, time=event.get("time", int(time.time()))) for event in events)
    save(data)
    if not pending:
        return
    try:
        snapshot = public_snapshot(data)
        snapshot["pending_count"] = 0  # These events are included in this message.
        print("  [通知] 发送 Telegram 事件，最多 25 秒…", flush=True)
        with deadline(25):
            telegram_monitor.notify(snapshot=snapshot, events=pending)
    except Exception as exc:
        # Notifications must never roll back a successful connection. Keep only
        # the exception type in logs; upstream errors might contain credentials.
        print(f"Telegram 事件通知未送达（{type(exc).__name__}）；已保存，下次检测重试。", flush=True)
        return
    data["pending_notifications"] = []
    save(data)


def active_name(data):
    return next((name(r) for r in data["nodes"] if r["id"] == data.get("active_id")), "")


def availability_events(data):
    previously = bool(data.get("availability_alert"))
    unavailable = not any(
        (r['id'] == data.get('active_id') and r['status'] == 'alive')
        or (r.get('reachability', 'unknown') != 'unreachable' and r['status'] != 'dead')
        for r in data['nodes'])
    # Empty inventories only alert if automatic connection was requested or
    # this is the continuation of an existing outage.
    unavailable = unavailable and bool(data["nodes"] or data.get("auto") or data.get("had_nodes") or previously)
    data["availability_alert"] = unavailable
    if unavailable != previously:
        return [{"kind": "all_unavailable" if unavailable else "recovered",
                 "reason": "本轮未找到可用节点" if unavailable else "本轮检测到可用节点"}]
    return []


@contextmanager
def locked(nonblocking=False):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as lock:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if nonblocking:
                    raise
                if time.monotonic() >= deadline:
                    raise ValueError("节点检测或切换正在进行，请稍后重试。")
                time.sleep(0.1)
        yield


def fingerprint(node):
    return hashlib.sha256(json.dumps(normalize(node), sort_keys=True).encode()).hexdigest()


def name(record):
    return record.get("custom_name") or record["name"]


def valid(node):
    return render(node, LIB / "templates" / f"{node.get('protocol')}.json", "off", "global")


def new_record(node, number=""):
    identity = fingerprint(node)
    return {"id": identity[:12], "fingerprint": identity, "node": node,
            "name": f"位置待查询-{LABELS[node['protocol']]}-{identity[:6]}",
            "source_number": str(number), "status": "unknown", "failures": 0,
            "latency_ms": None, "checked_at": None, "created_at": int(time.time())}


def unique_record(data, node, number=""):
    record = new_record(node, number)
    identities = {r["id"] for r in data["nodes"]}
    while record["id"] in identities:
        record["id"] = secrets.token_hex(6)
    record["name"] = f"位置待查询-{LABELS[node['protocol']]}-{record['id'][:6]}"
    return record


def active_protocol():
    try:
        return (STATE / "active").read_text().strip()
    except FileNotFoundError:
        return None


def running():
    return subprocess.run(["systemctl", "is-active", "--quiet", "sing-box.service"], timeout=5).returncode == 0


def test_record(record, geography=False):
    policy.transport_slot()
    return measure(record['node'])


def check_record(record, geography=False, count_failure=False):
    now = time.time()
    stamp = record.get('latency_checked_at') or 0
    if 0 <= now-stamp < policy.settings()['transport_cache_ttl']:
        print(f"{name(record)}：复用近期 {record.get('latency_method') or '服务器'} 测速结果", flush=True)
        return record.get('reachability') == 'reachable'
    print(f"正在测量 {name(record)} 的服务器延迟（不进行代理认证）…", flush=True)
    try:
        info = test_record(record)
    except policy.Deferred as exc:
        record['transport_error'] = str(exc)
        print(f'测速延后：{exc}；不计失败', flush=True)
        return None
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        info = {'reachability': 'unknown', 'latency_ms': None, 'transport_error': '测速工具或本机网络暂不可用'}
    record.update(info)
    record['latency_checked_at'] = now
    record['latency_method'] = 'ping' if record['node']['protocol'] == 'wg' else 'tcping'
    reachability = record.get('reachability', 'unknown')
    if reachability == 'reachable':
        record['failures'] = 0
        record.pop('transport_error', None)
        print(f"{name(record)}：服务器可达，{record['latency_method']} {record['latency_ms']} ms；代理以实际连接为准", flush=True)
        return True
    print(f"{name(record)}：{record.get('transport_error', '延迟未知')}", flush=True)
    return False if reachability == 'unreachable' else None


def migrate(data):
    """Keep existing single-profile settings when introducing the inventory."""
    if data.get("legacy_migrated"):
        return
    for protocol in LABELS:
        try:
            node = normalize(json.loads((PROFILES / f"{protocol}.json").read_text()))
            valid(node)
        except (ValueError, OSError, KeyError):
            continue
        identity = fingerprint(node)
        record = next((r for r in data["nodes"] if r["fingerprint"] == identity), None)
        if not record:
            record = unique_record(data, node, "原有配置")
            data["nodes"].append(record)
        if active_protocol() == protocol:
            data["active_id"] = record["id"]
    data["legacy_migrated"] = True
    save(data)


def import_file(data, path):
    path = Path(path).expanduser()
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("节点文件超过 5 MiB，请分批导入")
    blocks = parse_numbered(path.read_text(encoding="utf-8-sig"))
    if not blocks:
        raise ValueError("文件没有节点；请在每个节点前单独写一行编号")
    for number, node, error in blocks:
        if error:
            print(f"编号 {number}：{error}", flush=True)
            continue
        try:
            valid(node)
        except (ValueError, OSError, KeyError):
            print(f"编号 {number}：节点字段不完整或无效，未导入", flush=True)
            continue
        if any(fingerprint(node) in (r["fingerprint"], r.get("pending_fingerprint")) for r in data["nodes"]):
            print(f"编号 {number}：节点已存在，跳过重复项", flush=True)
            continue
        record = unique_record(data, node, number)
        print(f"编号 {number}：转换为 {LABELS[node['protocol']]}，正在测量服务器…", flush=True)
        check_record(record)
        if record.get('reachability') == 'unreachable':
            print('TCP 握手失败，已清除本次导入项。', flush=True)
            continue
        data["nodes"].append(record)
        save(data)
    print(f"节点库共 {len(data['nodes'])} 个节点；导入仅测服务器，实际连接后获取出口 IP 与中文名称。")


def select(data, identity):
    for record in data["nodes"]:
        if record["id"] == identity:
            return record
    raise ValueError("找不到该节点，请刷新节点列表")


def activate(data, record, apply_pending=True):
    print(f"正在连接 {name(record)}…", flush=True)
    pending = apply_pending and bool(record.get("pending_node"))
    node = record["pending_node"] if pending else record["node"]
    with tempfile.TemporaryDirectory(prefix="singbox-select-") as directory:
        path = Path(directory) / "node.json"
        write_json(path, node)
        result = subprocess.run(["bash", str(LIB / "runtime.sh"), "--activate-node", str(path)])
    record.pop('connection_deferred', None)
    if result.returncode == 75:
        record['connection_deferred'] = True
        print('实际验证暂缓；保留节点，等待本机检测额度或检测网站恢复。', flush=True)
        return False
    if result.returncode:
        if pending:
            for key in ('pending_node', 'pending_fingerprint', 'pending_error'):
                record.pop(key, None)
            print('待应用配置连接失败，已丢弃修改；原有可用节点继续保留。', flush=True)
        else:
            record["status"] = "dead"
            record["error"] = "实际连接失败，立即删除"
            remove_records(data, {record['id']})
            save(data)
            emit_events(data, [{'kind': 'deleted', 'reason': '实际连接失败，立即删除',
                                'deleted': [{'name': name(record), 'failures': record.get('failures', 0)}]}])
        return False
    if pending:
        commit_node(record, node)
    write_json(PROFILES / f"{node['protocol']}.json", node)
    data["active_id"] = record["id"]
    refresh_active(record)
    save(data, publish=False)
    return True


def refresh_active(record):
    record["status"], record["failures"] = "alive", 0
    record["watch_failures"] = 0
    record["proxy_checked_at"] = int(time.time())
    record.pop("error", None)
    try:
        info = json.loads((STATE / "probe-result.json").read_text())
        if info.get("protocol") == record["node"]["protocol"] and 0 <= time.time() - info.get("checked_at", 0) < 120:
            record.update({k: v for k, v in info.items() if k in (
                "ip", "city", "country", "country_code", "region", "org", "source", "location_zh", "latency_url", "checked_at")})
            record["http_latency_ms"] = info.get("latency_ms")
    except (OSError, ValueError, TypeError):
        pass
    record["checked_at"] = int(time.time())
    if not record.get("location_zh") or record.get("location_zh") == "位置待查询":
        try:
            if record.get("ip"):
                print("  [节点名称] 根据出口信息生成中文位置…", flush=True)
                record["location_zh"] = chinese_location(18081, record, timeout=8)
                record["name"] = f"{record['location_zh']}-{LABELS[record['node']['protocol']]}-{record['id'][:6]}"
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
            pass  # Activation already passed; geography cannot undo it.
    if record.get('location_zh') and record['location_zh'] != '位置待查询':
        record['name'] = f"{record['location_zh']}-{LABELS[record['node']['protocol']]}-{record['id'][:6]}"


def connect_best(data, exclude=None, events=None, reason="按 TCPing/Ping 延迟选择可用节点"):
    previous = active_name(data)
    events = [] if events is None else events
    failed = []
    # Fresh transport data only. Never rank by historical HTTP request duration.
    for record in list(data['nodes']):
        check_record(record, count_failure=False)
    prune_transport(data, events)
    candidates = sorted((r for r in data['nodes'] if r['id'] != exclude
                         and r.get('reachability') == 'reachable'),
                        key=lambda r: r.get('latency_ms') if r.get('latency_ms') is not None else float('inf'))
    # ICMP can be filtered; unknown WireGuard endpoints are tried last, never
    # pronounced dead from ping silence. TCP endpoints must complete a handshake.
    candidates += [r for r in data['nodes'] if r['id'] != exclude and r['node']['protocol'] == 'wg'
                   and r.get('reachability') == 'unknown']
    for record in candidates:
        if activate(data, record, apply_pending=False):
            if events is not None:
                events.append({'kind': 'failover' if exclude else 'auto_connected', 'reason': reason,
                               'previous': previous, 'current': name(record), 'failed': failed})
            return True
        if record.get('connection_deferred'):
            if events is not None:
                events.append({'kind': 'probe_deferred', 'reason': '本机频率限制或检测暂不可用；保留节点稍后重试'})
            break
        failed.append(name(record))
    if events is not None and failed:
        events.append({'kind': 'connect_failed', 'reason': reason, 'previous': previous,
                       'current': active_name(data), 'failed': failed})
    print('当前候选未能连接；自动模式会在下一轮继续检测。', flush=True)
    return False


def stop_runtime():
    subprocess.run(["bash", str(LIB / "runtime.sh"), "--stop-node"], check=True)


def remove_records(data, identities):
    data['had_nodes'] = bool(data['nodes']) or data.get('had_nodes', False)
    deleted = [{"name": name(r), "failures": r.get("failures", 0)} for r in data["nodes"] if r["id"] in identities]
    if data.get("active_id") in identities:
        stop_runtime()
        data["active_id"] = None
        CONFIG.unlink(missing_ok=True)
    for record in [r for r in data["nodes"] if r["id"] in identities]:
        protocol = record["node"]["protocol"]
        path = PROFILES / f"{protocol}.json"
        try:
            if fingerprint(json.loads(path.read_text())) == record["fingerprint"]:
                write_json(path, json.loads((LIB / "nodes" / f"{protocol}.json").read_text()))
        except FileNotFoundError:
            pass
        data["nodes"].remove(record)
        print(f"已删除节点：{name(record)}", flush=True)
    return deleted


def prune_transport(data, events):
    # A healthy running connection has stronger evidence than a failed new TCP
    # handshake. Its watchdog will confirm application-level failures separately.
    doomed = {r['id'] for r in data['nodes'] if r.get('reachability') == 'unreachable'
              and r['node']['protocol'] != 'wg' and r['id'] != data.get('active_id')}
    if doomed:
        deleted = remove_records(data, doomed)
        events.append({'kind': 'deleted', 'reason': '服务器 TCP 握手失败，立即清除', 'deleted': deleted})
        save(data)


def scan(data, scheduled=False, choose_best=False, events=None):
    events = [] if events is None else events
    if choose_best:
        subprocess.run(['bash', str(LIB/'runtime.sh'), '--start-node-monitor'], check=False)
    records = list(data['nodes'])
    if scheduled:
        now = time.time()
        config = policy.settings()
        if now-data.get('last_standby_check', 0) < config['standby_gap']:
            return
        due = [r for r in records if r['id'] != data.get('active_id')
               and now-(r.get('latency_checked_at') or 0) >= config['standby_interval']]
        records = sorted(due, key=lambda r: r.get('latency_checked_at') or 0)[:1]
        if not records:
            if data.get('pending_notifications'):
                emit_events(data, [])
            return
        data['last_standby_check'] = int(now)
    for record in records:
        check_record(record)
        save(data, publish=False)
    prune_transport(data, events)
    if choose_best:
        connect_best(data, events=events, reason='按 TCPing/Ping 升序连接；失败立即清除并尝试下一节点')
    events.extend(availability_events(data))
    data['last_scan'] = int(time.time())
    save(data)
    emit_events(data, events)


def manual_connect(data, identity, reason="用户手动连接指定节点"):
    previous = active_name(data)
    record = select(data, identity)
    success = activate(data, record)
    if success:
        data["auto"] = True
    events = [{"kind": "connected" if success else "connect_failed", "reason": reason,
               "previous": previous, "current": active_name(data), "failed": [] if success else [name(record)]}]
    events.extend(availability_events(data))
    save(data)
    emit_events(data, events)
    if not success and (not data.get("active_id") or not running()):
        # No usable previous service to preserve: keep trying the inventory.
        data["auto"] = True
        save(data)
        watch(data)
        selected = next((r for r in data["nodes"] if r["id"] == data.get("active_id")), None)
        return bool(selected and selected["status"] == "alive" and running())
    return success


def watch(data):
    """Reuse the active proxy; never authenticate all standby nodes."""
    if not data.get('auto') or data.get('deferred_until', 0) > time.time():
        return
    active = next((r for r in data['nodes'] if r['id'] == data.get('active_id')), None)
    service_up = running()
    if active and service_up:
        if 0 <= time.time() - active.get('proxy_checked_at', 0) < policy.settings()['cache_ttl']:
            return
        try:
            with policy.operation('current'):
                try:
                    info = health(18081, attempts=1, timeout=12)
                except RuntimeError:
                    log = subprocess.run(['journalctl','-u','sing-box','--since','-20 seconds','-n','100','--no-pager'],
                                         capture_output=True,text=True,timeout=5)
                    restriction = upstream_restriction(log.stdout)
                    if restriction:
                        raise restriction
                    raise
        except policy.Deferred as exc:
            if data.get('deferred_until', 0) < time.time():
                data['deferred_until'] = exc.retry_at
                emit_events(data, [{'kind': 'probe_deferred', 'reason': str(exc), 'current': name(active)}])
            save(data)
            return
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
            # health() already tried the independent fallback URL. Confirmed
            # HTTPS failure is removed now, without keeping failed-round counts.
            pass
        else:
            active.update(status='alive', watch_failures=0, proxy_checked_at=int(time.time()),
                          http_latency_ms=info.get('latency_ms'))
            active.pop('error', None)
            try:
                policy.cache_put(active['fingerprint'], info, active=True)
            except (OSError, ValueError):
                print('检测成功；本机缓存暂不可写，保留当前连接。', flush=True)
            save(data)
            if data.get('pending_notifications'):
                emit_events(data, [])
            return
    events = []
    previous = name(active) if active else ''
    confirmed = bool(active and service_up)
    if confirmed:
        active['status'] = 'dead'
        deleted = remove_records(data, {active['id']})
        events.append({'kind': 'deleted', 'reason': '当前代理两个 HTTPS 地址均连接失败，立即删除', 'deleted': deleted})
        save(data)
    # A stopped local service alone does not prove that the node is invalid.
    print('当前连接不可用，按 TCPing/Ping 延迟尝试；实际连接失败的候选立即删除。', flush=True)
    connect_best(data, exclude=active['id'] if confirmed else None, events=events,
                 reason='当前代理连接失败；按服务器延迟依次尝试')
    if previous:
        for event in events:
            if event['kind'] in ('failover', 'auto_connected'):
                event.update(kind='failover', previous=previous)
    events.extend(availability_events(data))
    save(data)
    emit_events(data, events)


def profile_node(protocol):
    if protocol not in LABELS:
        raise ValueError("未知代理协议")
    node = normalize(json.loads((PROFILES / f"{protocol}.json").read_text()))
    if node["protocol"] != protocol:
        raise ValueError("单节点配置的协议不匹配")
    valid(node)
    return node


def upsert_profile(data, protocol):
    node = profile_node(protocol)
    identity = fingerprint(node)
    record = next((r for r in data["nodes"] if identity in (r["fingerprint"], r.get("pending_fingerprint"))), None)
    if record is None:
        record = unique_record(data, node, "主菜单配置")
        data["nodes"].append(record)
    elif record["fingerprint"] != identity:
        commit_node(record, node)
    return record


def protocol_options(data, protocol, editing=False):
    if protocol not in LABELS:
        raise ValueError("未知代理协议")
    records = [r for r in data["nodes"] if r["node"]["protocol"] == protocol]
    try:
        node = profile_node(protocol)
    except (ValueError, OSError, KeyError):
        if editing:
            records.append({"id": None, "name": f"填写新的 {LABELS[protocol]} 节点", "node": {"protocol": protocol}, "from_profile": True})
        return records
    if not any(fingerprint(node) in (r["fingerprint"], r.get("pending_fingerprint")) for r in records):
        record = new_record(node, "主菜单配置")
        record["from_profile"] = True
        records.append(record)
    return records


def choose_protocol(protocol, editing=False):
    data = load()
    records = protocol_options(data, protocol, editing)
    if not records:
        edit_option = {"https": 2, "shadowsocks": 3, "wg": 4, "socks5": 14}[protocol]
        print(f"尚无 {LABELS[protocol]} 节点。请在菜单 17 导入，或在菜单 {edit_option} 填写节点配置。")
        return False
    page = 0
    while True:
        print(render_protocol(data, protocol, records, page, editing=editing))
        raw = input("选择节点编号（0 返回） › ").strip()
        if not raw or raw in ("0", "00"):
            return False
        if not raw.isdecimal():
            print("请输入列表中的数字编号。")
            continue
        index = int(raw)
        pages = (len(records) + PAGE_SIZE - 1) // PAGE_SIZE
        if pages > 1 and index in (len(records)+1, len(records)+2):
            page = max(0, min(pages - 1, page + (-1 if index == len(records)+1 else 1)))
            continue
        if not 1 <= index <= len(records):
            print("节点编号无效，请重新选择。")
            continue
        return records[index-1]


def connect_protocol(protocol):
    chosen = choose_protocol(protocol)
    if not chosen:
        return False
    with locked():
        fresh = load()
        if chosen.get("from_profile"):
            if fingerprint(profile_node(protocol)) != chosen["fingerprint"]:
                raise ValueError("单节点配置已变化，请重新打开协议菜单")
            record = upsert_profile(fresh, protocol)
        else:
            record = select(fresh, chosen["id"])
            if record["node"]["protocol"] != protocol:
                raise ValueError("节点协议已变化，请重新选择")
        return manual_connect(fresh, record["id"], reason=f"主菜单切换 {LABELS[protocol]} 代理（手动选择）")


def commit_node(record, node):
    record["node"], record["fingerprint"] = node, fingerprint(node)
    for key in ("pending_node", "pending_fingerprint", "pending_error", "ip", "location_zh", "city", "region", "country", "org", "error"):
        record.pop(key, None)
    record.update(status="unknown", failures=0, latency_ms=None, checked_at=None,
                  latency_method=None, latency_checked_at=None, reachability="unknown", proxy_checked_at=0)
    record["name"] = f"位置待查询-{LABELS[node['protocol']]}-{record['id'][:6]}"


def sync_edited_profile(data, record):
    protocol = record["node"]["protocol"]
    active = next((r for r in data["nodes"] if r["id"] == data.get("active_id")), None)
    if (active and active["node"]["protocol"] == protocol) or (active_protocol() == protocol and running()):
        return  # Keep the selected runtime profile for restart/mode changes.
    write_json(PROFILES / f"{protocol}.json", record["node"])


def save_node_edit(data, identity, node):
    node = normalize(node)
    valid(node)
    record = select(data, identity)
    if node["protocol"] != record["node"]["protocol"]:
        raise ValueError("编辑不能改变节点协议，请另行导入")
    target = fingerprint(node)
    duplicate = next((r for r in data["nodes"] if r["id"] != identity and target in (r["fingerprint"], r.get("pending_fingerprint"))), None)
    if duplicate:
        raise ValueError(f"配置与已有节点 {name(duplicate)} 重复，请直接选择该节点")
    if identity == data.get("active_id"):
        if target == record["fingerprint"]:
            record.pop("pending_node", None)
            record.pop("pending_fingerprint", None)
            record.pop("pending_error", None)
            reason = "当前配置已保留，待应用修改已清除"
        else:
            record["pending_node"], record["pending_fingerprint"] = node, target
            record.pop("pending_error", None)
            reason = "运行中节点的修改已保存为待应用；手动连接成功后生效"
    else:
        commit_node(record, node)
        sync_edited_profile(data, record)
        reason = "节点资料已保存，等待检测或连接"
    save(data)
    emit_events(data, [{"kind": "edited", "reason": reason, "current": name(record)}])
    print(reason)


def edit_selected(chosen):
    protocol = chosen["node"]["protocol"]
    source = PROFILES / f"{protocol}.json"
    original_bytes = source.read_bytes() if chosen.get("from_profile") else None
    original = json.loads(original_bytes) if original_bytes is not None else chosen.get("pending_node", chosen["node"])
    expected = None if original_bytes is not None else fingerprint(original)
    with tempfile.TemporaryDirectory(prefix="singbox-edit-") as directory:
        path = Path(directory) / "node.json"
        write_json(path, original)
        subprocess.run(["python3", str(LIB / "edit_node.py"), protocol, str(path)], check=True)
        node = normalize(json.loads(path.read_text()))
        valid(node)
    with locked():
        fresh = load()
        if original_bytes is not None:
            if source.read_bytes() != original_bytes:
                raise ValueError("配置在编辑期间已被修改，请刷新后再编辑")
            if any(fingerprint(node) in (r["fingerprint"], r.get("pending_fingerprint")) for r in fresh["nodes"]):
                print("此配置已在节点库中，请直接选择对应节点。")
                return
            record = unique_record(fresh, node, "主菜单编辑")
            fresh["nodes"].append(record)
            sync_edited_profile(fresh, record)
            save(fresh)
            emit_events(fresh, [{"kind": "edited", "reason": "主菜单新增节点，等待检测或连接", "current": name(record)}])
            print("节点已加入节点库，请选择连接或检测。")
        else:
            record = select(fresh, chosen["id"])
            if fingerprint(record.get("pending_node", record["node"])) != expected:
                raise ValueError("节点在编辑期间已被修改，请刷新后再编辑")
            save_node_edit(fresh, record["id"], node)


def manual_stop(data):
    previous = active_name(data)
    try:
        stop_runtime()
    except (OSError, subprocess.SubprocessError):
        emit_events(data, [{"kind": "stop_failed", "reason": "主菜单关闭代理失败，请检查服务状态", "current": previous}])
        raise
    data["auto"], data["active_id"] = False, None
    save(data)
    try:
        emit_events(data, [{"kind": "stopped", "reason": "主菜单手动关闭代理，自动连接已关闭", "previous": previous}])
    finally:
        subprocess.run(["bash", str(LIB / "runtime.sh"), "--stop-node-monitor"], check=True)


def mode_event(data, success, reason):
    previous = active_name(data)
    if success:
        record = upsert_profile(data, active_protocol())
        data["active_id"] = record["id"]
        refresh_active(record)
    events = [{"kind": "mode_changed" if success else "mode_failed", "reason": reason,
               "previous": previous, "current": active_name(data)}]
    if success:
        events.extend(availability_events(data))
    save(data)
    emit_events(data, events)


def display(data, page=0):
    print(render_inventory(data, page))


def choose_identity(data):
    raw = input("节点编号（回车取消）：").strip()
    if not raw:
        return None
    if not raw.isdecimal() or not 1 <= int(raw) <= len(data["nodes"]):
        raise ValueError("节点编号无效")
    return data["nodes"][int(raw) - 1]["id"]


def menu():
    page = 0
    while True:
        data = load()
        page = min(page, max(0, (len(data["nodes"])-1) // PAGE_SIZE))
        display(data, page)
        action = input("多节点管理 › ").strip()
        if action.isdecimal():
            action = str(int(action))
        try:
            if action == "0":
                return
            if action in ("11", "12"):
                page = max(0, min(max(0, (len(data["nodes"])-1) // PAGE_SIZE), page + (-1 if action == "11" else 1)))
                continue
            if action == "1":
                print(f"将所有节点放入 {IMPORT_FILE}，每个节点前单独写一行编号 1、2、3…")
                raw = input(f"导入文件 [{IMPORT_FILE}]：").strip()
                with locked():
                    import_file(load(), raw or IMPORT_FILE)
            elif action in ("2", "3", "4", "8", "9"):
                identity = choose_identity(data)
                if not identity:
                    continue
                if action == "2":
                    record = select(data, identity)
                    node = dict(record.get("pending_node", record["node"]))
                    if record.get("pending_node"):
                        print("以下为待应用配置；当前连接与自动巡检仍使用原配置。")
                    for field in ("password", "private_key", "pre_shared_key"):
                        if node.get(field):
                            node[field] = "（已填写，隐藏）"
                    print(json.dumps(node, ensure_ascii=False, indent=2))
                elif action == "3":
                    edit_selected(select(load(), identity))
                elif action == "4":
                    with locked():
                        fresh = load()
                        manual_connect(fresh, identity)
                elif action == "8":
                    with locked():
                        fresh = load()
                        deleted = remove_records(fresh, {identity})
                        events = [{"kind": "deleted", "reason": "用户手动删除", "deleted": deleted}]
                        if fresh.get("auto") and not fresh.get("active_id"):
                            scan(fresh, events=events)
                        else:
                            events.extend(availability_events(fresh))
                            save(fresh)
                            emit_events(fresh, events)
                else:
                    custom = input("新名称（回车恢复地理位置名称）：").strip()
                    if len(custom) > 80 or any(ord(c) < 32 for c in custom):
                        raise ValueError("名称最多 80 字，不能包含控制字符")
                    with locked():
                        fresh = load()
                        select(fresh, identity)["custom_name"] = custom
                        save(fresh)
            elif action in ("5", "6", "7"):
                with locked():
                    fresh = load()
                    if action == "6":
                        fresh["auto"] = False
                        save(fresh)
                        print("自动连接已关闭；当前代理继续运行。")
                    else:
                        if action == "5":
                            fresh["auto"] = True
                            save(fresh)
                        scan(fresh, choose_best=action == "5")
            elif action == "10":
                current = policy.settings()['standby_interval']
                raw = input(f"同一备用节点复测间隔（秒，至少60；当前{current}）：").strip() or str(current)
                if not raw.isdecimal() or not 60 <= int(raw) <= 604800:
                    raise ValueError('请填写 60–604800 秒')
                config = policy.settings()
                config['standby_interval'] = int(raw)
                policy.atomic(policy.CONFIG, config)
                policy.CONFIG.chmod(0o640)
                print('已保存本机巡检间隔；每次轮询最多测一个备用节点。')
            else:
                print("请选择菜单中显示的功能编号")
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"操作失败：{exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "menu", "import", "scan", "monitor", "watch", "auto", "manual", "summary", "connect-protocol", "edit-protocol", "stop", "mode-ok", "mode-failed"))
    parser.add_argument("file", nargs="?")
    args = parser.parse_args()
    if args.command == "menu":
        menu()
        return
    if args.command == "connect-protocol":
        connect_protocol(args.file)
        return
    if args.command == "edit-protocol":
        chosen = choose_protocol(args.file, editing=True)
        if chosen:
            edit_selected(chosen)
        return
    if args.command == "stop":
        with locked():
            manual_stop(load())
        return
    if args.command == "summary":
        data = load()
        active = next((name(r) for r in data["nodes"] if r["id"] == data.get("active_id")), "未选择")
        print(f"{len(data['nodes'])} 个节点 · 自动连接 {'开启' if data.get('auto') else '关闭'} · {active}")
        return
    # These callbacks are called by shell operations already holding the lock.
    if args.command in ("init", "manual", "mode-ok", "mode-failed"):
        data = load()
        if args.command == "init":
            migrate(data)
            if active_protocol() in LABELS and running():
                try:
                    data["active_id"] = upsert_profile(data, active_protocol())["id"]
                except (ValueError, OSError, KeyError):
                    pass
            save(data)
        elif args.command.startswith("mode-"):
            mode_event(data, args.command == "mode-ok", args.file or "主菜单切换运行模式")
        else:
            data["auto"] = False
            data["active_id"] = None
            save(data)
        return
    try:
        with locked(nonblocking=args.command in ("monitor", "watch")):
            data = load()
            if args.command == "watch":
                watch(data)
            elif args.command == "import":
                import_file(data, args.file or IMPORT_FILE)
            else:
                if args.command == "auto":
                    data["auto"] = True
                    save(data)
                scan(data, scheduled=args.command == "monitor", choose_best=args.command == "auto")
    except BlockingIOError:
        print("其他节点操作正在进行，本轮定时检测跳过。")


if __name__ == "__main__":
    from utf8_runtime import ensure_utf8
    ensure_utf8()
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"节点管理失败：{exc}")
        raise SystemExit(1)
