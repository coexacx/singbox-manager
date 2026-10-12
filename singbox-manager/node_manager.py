#!/usr/bin/env python3
"""Persistent node inventory, import, health monitoring and sticky failover."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from node_formats import normalize, parse_numbered
from node_health import health, inspect
from probe import probe
from render import render

LIB = Path(__file__).resolve().parent
PROFILES = Path("/etc/sing-box/manager")
STATE = Path("/var/lib/singbox-manager")
DATABASE = PROFILES / "node-store.json"
IMPORT_FILE = PROFILES / "nodes.txt"
LOCK = Path("/run/lock/singbox-manager.lock")
CONFIG = Path("/etc/sing-box/config.json")
LABELS = {"wg": "WireGuard", "https": "HTTPS", "shadowsocks": "Shadowsocks", "socks5": "SOCKS5"}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".nodes-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load():
    if not DATABASE.exists():
        return {"version": 1, "nodes": [], "auto": False, "active_id": None, "delete_after": 3}
    data = json.loads(DATABASE.read_text())
    if data.get("version") != 1 or not isinstance(data.get("nodes"), list):
        raise ValueError("节点数据库格式无效，请检查备份")
    return data


def save(data):
    write_json(DATABASE, data)


@contextmanager
def locked(nonblocking=False):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
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


def active_protocol():
    try:
        return (STATE / "active").read_text().strip()
    except FileNotFoundError:
        return None


def running():
    return subprocess.run(["systemctl", "is-active", "--quiet", "sing-box.service"]).returncode == 0


def is_current_profile(node):
    protocol = active_protocol()
    if protocol != node["protocol"] or not running():
        return False
    try:
        current = json.loads((PROFILES / f"{protocol}.json").read_text())
        if fingerprint(current) == fingerprint(node):
            return True
        if protocol == "wg":
            return all(current.get(key) == node.get(key) for key in ("private_key", "peer_public_key", "peer_address", "peer_port"))
        return False
    except (OSError, ValueError):
        return False


def test_record(record, geography=False):
    node = record["node"]
    # A second WireGuard session with the same key can steal the peer endpoint
    # from the running tunnel. Test the active node through its own SOCKS port.
    if is_current_profile(node):
        return inspect(18081, geography)
    config = valid(node)
    with tempfile.TemporaryDirectory(prefix="singbox-node-") as directory:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        return probe(path, inspector=lambda port: inspect(port, geography), quiet=True)


def check_record(record, geography=False, count_failure=False):
    record["checked_at"] = int(time.time())
    try:
        info = test_record(record, geography)
        record.update(info)
        record["status"], record["failures"] = "alive", 0
        record.pop("error", None)
        if info.get("location_zh"):
            record["name"] = f"{info['location_zh']}-{LABELS[record['node']['protocol']]}-{record['id'][:6]}"
        print(f"{name(record)}：存活，HTTP 耗时 {record['latency_ms']} ms，IP {record.get('ip', '待查询')}", flush=True)
        return True
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        record["status"], record["latency_ms"] = "dead", None
        record["error"] = "节点连接或 HTTP 检测失败"
        if count_failure:
            record["failures"] = record.get("failures", 0) + 1
        print(f"{name(record)}：检测失败，连续失败 {record['failures']} 轮", flush=True)
        return False


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
            record = new_record(node, "原有配置")
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
        if any(r["fingerprint"] == fingerprint(node) for r in data["nodes"]):
            print(f"编号 {number}：节点已存在，跳过重复项", flush=True)
            continue
        record = new_record(node, number)
        print(f"编号 {number}：转换为 {LABELS[node['protocol']]}，正在测试…", flush=True)
        check_record(record, geography=True)
        data["nodes"].append(record)
        save(data)
    print(f"节点库共 {len(data['nodes'])} 个节点；连接失败的导入项保留待复测。")


def select(data, identity):
    for record in data["nodes"]:
        if record["id"] == identity:
            return record
    raise ValueError("找不到该节点，请刷新节点列表")


def activate(data, record):
    print(f"正在连接 {name(record)}…", flush=True)
    with tempfile.TemporaryDirectory(prefix="singbox-select-") as directory:
        path = Path(directory) / "node.json"
        write_json(path, record["node"])
        result = subprocess.run(["bash", str(LIB / "runtime.sh"), "--activate-node", str(path)])
    if result.returncode:
        record["status"] = "dead"
        record["error"] = "连接失败；已保留先前配置"
        return False
    write_json(PROFILES / f"{record['node']['protocol']}.json", record["node"])
    data["active_id"] = record["id"]
    record["status"], record["failures"] = "alive", 0
    if (STATE / "exit_ip").exists():
        record["ip"] = (STATE / "exit_ip").read_text().strip()
    save(data)
    return True


def connect_best(data, exclude=None):
    candidates = sorted((r for r in data["nodes"] if r["status"] == "alive" and r["id"] != exclude),
                        key=lambda r: r.get("latency_ms") if r.get("latency_ms") is not None else float("inf"))
    # If only the runtime process failed, retry the same reachable node after
    # trying other nodes. A single-node installation must also recover.
    candidates += [r for r in data["nodes"] if r["id"] == exclude and r["status"] == "alive"]
    for record in candidates:
        if activate(data, record):
            return True
    print("没有能够连接的存活节点；自动模式会在下一轮检测后重试。", flush=True)
    return False


def stop_runtime():
    subprocess.run(["bash", str(LIB / "runtime.sh"), "--stop-node"], check=True)


def remove_records(data, identities):
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


def scan(data, scheduled=False, choose_best=False):
    for record in data["nodes"]:
        need_geo = not record.get("ip") or record.get("location_zh", "位置待查询") == "位置待查询"
        check_record(record, geography=need_geo, count_failure=scheduled)
        save(data)
    active = next((r for r in data["nodes"] if r["id"] == data.get("active_id")), None)
    if data.get("auto"):
        broken = active is None or active["status"] != "alive" or not running()
        if not broken and not choose_best:
            try:
                health(18081)
            except (OSError, RuntimeError):
                broken = True
        if choose_best or broken:
            # Exclude the failed active node even if its standalone probe works.
            connect_best(data, exclude=active["id"] if broken and active else None)
    if scheduled:
        doomed = {r["id"] for r in data["nodes"] if r.get("failures", 0) >= data.get("delete_after", 3)}
        if doomed:
            remove_records(data, doomed)
    data["last_scan"] = int(time.time())
    save(data)


def display(data):
    print("\n╭────────────── 多节点管理 ──────────────╮")
    print("编号  名称 / 协议 / 存活 / HTTP 耗时 / 连续失败 / 出口 IP")
    for index, record in enumerate(data["nodes"], 1):
        marker = " ▶" if record["id"] == data.get("active_id") else ""
        status = {"alive": "存活", "dead": "失败", "unknown": "未检测"}.get(record["status"], "未知")
        latency = f"{record['latency_ms']} ms" if record.get("latency_ms") is not None else "—"
        print(f"{index:>3}  {name(record)}{marker} | {LABELS[record['node']['protocol']]} | {status} | {latency} | {record.get('failures', 0)} 轮 | {record.get('ip', '—')}")
    print(f"自动连接：{'开启' if data.get('auto') else '关闭'}；每 30 分钟巡检，连续 {data.get('delete_after', 3)} 轮失败删除。")
    if data.get("last_scan"):
        print("上次全库检测：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["last_scan"])))


def choose_identity(data):
    raw = input("节点编号（回车取消）：").strip()
    if not raw:
        return None
    if not raw.isdecimal() or not 1 <= int(raw) <= len(data["nodes"]):
        raise ValueError("节点编号无效")
    return data["nodes"][int(raw) - 1]["id"]


def menu():
    while True:
        data = load()
        display(data)
        print("\n1 批量导入  2 查看节点  3 编辑节点  4 连接指定节点（手动）")
        print("5 自动连接最低延迟节点  6 关闭自动连接  7 立即检测全部节点")
        print("8 删除节点  9 修改节点名称  10 设置失败删除轮数  0 返回")
        action = input("多节点管理 › ").strip()
        try:
            if action == "0":
                return
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
                    node = dict(select(data, identity)["node"])
                    for field in ("password", "private_key", "pre_shared_key"):
                        if node.get(field):
                            node[field] = "（已填写，隐藏）"
                    print(json.dumps(node, ensure_ascii=False, indent=2))
                elif action == "3":
                    old = select(data, identity)
                    with tempfile.TemporaryDirectory(prefix="singbox-edit-") as directory:
                        path = Path(directory) / "node.json"
                        write_json(path, old["node"])
                        subprocess.run(["python3", str(LIB / "edit_node.py"), old["node"]["protocol"], str(path)], check=True)
                        node = normalize(json.loads(path.read_text()))
                        valid(node)
                    with locked():
                        fresh = load()
                        record = select(fresh, identity)
                        record["node"], record["fingerprint"] = node, fingerprint(node)
                        record["failures"] = 0
                        for key in ("ip", "location_zh"):
                            record.pop(key, None)
                        check_record(record, geography=True)
                        save(fresh)
                    print("已保存；正在运行的配置保持到下次连接。")
                elif action == "4":
                    with locked():
                        fresh = load()
                        if activate(fresh, select(fresh, identity)):
                            fresh["auto"] = False
                        save(fresh)
                elif action == "8":
                    with locked():
                        fresh = load()
                        remove_records(fresh, {identity})
                        if fresh.get("auto") and not fresh.get("active_id"):
                            scan(fresh)
                        save(fresh)
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
                raw = input("连续失败几轮删除（1=本轮重试3次失败即删，默认3）：").strip() or "3"
                if not raw.isdecimal() or not 1 <= int(raw) <= 48:
                    raise ValueError("请填写 1–48")
                with locked():
                    fresh = load()
                    fresh["delete_after"] = int(raw)
                    save(fresh)
            else:
                print("请选择 0–10")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"操作失败：{exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "menu", "import", "scan", "monitor", "auto", "manual", "summary"))
    parser.add_argument("file", nargs="?")
    args = parser.parse_args()
    if args.command == "menu":
        menu()
        return
    if args.command == "summary":
        data = load()
        active = next((name(r) for r in data["nodes"] if r["id"] == data.get("active_id")), "未选择")
        print(f"{len(data['nodes'])} 个节点 · 自动连接 {'开启' if data.get('auto') else '关闭'} · {active}")
        return
    # init/manual are called by a shell operation already holding the same lock.
    if args.command in ("init", "manual"):
        data = load()
        if args.command == "init":
            migrate(data)
        else:
            data["auto"] = False
            data["active_id"] = None
            save(data)
        return
    try:
        with locked(nonblocking=args.command == "monitor"):
            data = load()
            if args.command == "import":
                import_file(data, args.file or IMPORT_FILE)
            else:
                if args.command == "auto":
                    data["auto"] = True
                    save(data)
                scan(data, scheduled=args.command == "monitor", choose_best=args.command == "auto")
    except BlockingIOError:
        print("其他节点操作正在进行，本轮定时检测跳过。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"节点管理失败：{exc}")
        raise SystemExit(1)
