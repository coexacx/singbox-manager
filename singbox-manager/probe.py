#!/usr/bin/env python3
"""Test a selected node through a temporary local SOCKS listener."""

import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import copy
import os

from ip_lookup import lookup_proxy_ip
import probe_policy as policy
from probe_errors import upstream_restriction

EXIT_IP_FILE = pathlib.Path("/var/lib/singbox-manager/exit_ip")
RESULT_FILE = EXIT_IP_FILE.with_name("probe-result.json")


def say(message):
    print(message, flush=True)


def test_exit_ip(port, protocol, include_ip=True):
    from node_health import health, chinese_location
    say("  [连通性] 一次 HTTPS 检测，失败才使用备用地址（最多 12 秒）…")
    info = health(port, attempts=1, timeout=12, progress=lambda text: say("    " + text))
    if include_ip:
        say("  [出口信息] 查询代理 IP，最多 15 秒…")
        try:
            info.update(lookup_proxy_ip(port, rounds=1, timeout=15, progress=lambda text: say("    " + text)))
            info['location_zh'] = chinese_location(port, info)
        except (RuntimeError, OSError):
            say("  代理 HTTPS 连通正常；IP 查询暂不可用，稍后监测会重试。")
    info["checked_at"] = int(time.time())
    return info


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def physical_interface():
    for family in ("-4", "-6"):
        result = subprocess.run(["ip", family, "route", "show", "default", "table", "main"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            words = result.stdout.split()
            if "dev" in words:
                return words[words.index("dev") + 1]
    return None


def probe(config_path, inspector=None, quiet=False):
    full = json.loads(pathlib.Path(config_path).read_text())
    tags = {"https": "https-out", "shadowsocks": "shadowsocks-out", "wg": "surfshark-wg", "socks5": "socks5-out"}
    outbound_tags = [item.get("tag") for item in full.get("outbounds", [])]
    outbound_tags += [item.get("tag") for item in full.get("endpoints", [])]
    protocol = next((name for name, tag in tags.items() if tag in outbound_tags), None)
    if protocol is None:
        raise ValueError("无法识别节点协议")
    port = available_port()
    direct_dns = next(
        (item["server"] for item in full["dns"]["servers"]
         if item.get("tag") in ("dns-direct", "surfshark-bootstrap")),
        "1.1.1.1",
    )
    config = {
        "log": {"level": "error"},
        "inbounds": [{"type": "socks", "tag": "probe-in", "listen": "127.0.0.1", "listen_port": port}],
        "outbounds": copy.deepcopy(full["outbounds"]),
        "dns": {
            "servers": [{"type": "udp", "tag": "probe-dns", "server": direct_dns}],
            "final": "probe-dns", "strategy": "ipv4_only",
        },
        "route": {
            "auto_detect_interface": True,
            "default_mark": hex(__import__("transport_probe").direct_mark()),
            "default_domain_resolver": "probe-dns",
            "final": tags[protocol],
        },
    }
    interface = physical_interface()
    if interface:
        for outbound in config["outbounds"]:
            if outbound.get("tag") == tags[protocol]:
                outbound["bind_interface"] = interface
    if protocol == "wg":
        config["endpoints"] = copy.deepcopy(full["endpoints"])
        for endpoint in config["endpoints"]:
            endpoint["domain_resolver"] = {"server": "probe-dns", "strategy": "ipv4_only"}
            if interface:
                endpoint["bind_interface"] = interface
    with tempfile.TemporaryDirectory(prefix="singbox-probe-") as directory:
        candidate = pathlib.Path(directory) / "config.json"
        candidate.write_text(json.dumps(config))
        checked = subprocess.run(["sing-box", "check", "-c", str(candidate)], capture_output=True, text=True, timeout=10)
        if checked.returncode:
            raise RuntimeError("节点测试配置无效：" + checked.stderr.strip()[:500])
        with open(pathlib.Path(directory) / "sing-box.log", "w+") as log:
            process = subprocess.Popen(["sing-box", "run", "-c", str(candidate)], stdout=log, stderr=log)
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError("节点测试进程启动失败：" + log.read()[:500])
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("节点测试监听未就绪")
                if protocol == "wg":
                    time.sleep(3)
                info = inspector(port) if inspector else test_exit_ip(port, protocol, include_ip=False)
                if not quiet:
                    say(f"节点测试通过：{protocol} HTTPS 连通，出口 IP：{info.get('ip', '暂未查询到')}")
                return info if inspector else info.get("ip")
            except RuntimeError:
                log.flush(); log.seek(0)
                restriction = upstream_restriction(log.read()[-16000:])
                if restriction:
                    raise restriction
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def check_active(protocol):
    if protocol not in ("https", "shadowsocks", "wg", "socks5"):
        raise ValueError("未知协议")
    active = json.loads(pathlib.Path("/etc/sing-box/config.json").read_text())
    inbound_types = {item.get("type") for item in active.get("inbounds", [])}
    if not ("tun" in inbound_types or "tproxy" in inbound_types):
        raise RuntimeError("未找到透明接管入口")

    tun_name = next((item.get("interface_name") or "sb-split0" for item in active["inbounds"]
                     if item.get("type") == "tun"), "sb-split0")
    ready = False
    say("  [入口就绪] 等待 TUN/透明代理及本地检测端口，最多 30 秒…")
    deadline = time.monotonic() + 30
    next_notice = time.monotonic() + 5
    while time.monotonic() < deadline:
        if time.monotonic() >= next_notice:
            say(f"    入口仍在启动，剩余 {max(0, int(deadline-time.monotonic()))} 秒…")
            next_notice = time.monotonic() + 5
        try:
            with socket.create_connection(("127.0.0.1", 18081), timeout=0.2):
                pass
        except OSError:
            time.sleep(0.5)
            continue
        if "tun" in inbound_types:
            link = subprocess.run(["ip", "link", "show", tun_name], capture_output=True, timeout=3)
            ready = link.returncode == 0
        else:
            table = subprocess.run(["nft", "list", "table", "inet", "singbox_manager"], capture_output=True, timeout=3)
            rules = subprocess.run(["ip", "-4", "rule", "show"], capture_output=True, text=True, timeout=3)
            ready = (table.returncode == 0 and rules.returncode == 0
                     and "fwmark 0x4001/0x4001 lookup 18082" in rules.stdout)
        if ready:
            break
        time.sleep(0.5)
    if not ready:
        if "tun" in inbound_types:
            if not pathlib.Path("/dev/net/tun").exists():
                raise policy.Deferred("TUN 设备 /dev/net/tun 不存在；请在服务器宿主机开放 TUN")
            raise policy.Deferred(f"TUN 接口 {tun_name} 或服务检测入口在 30 秒内未就绪；请检查 sing-box 服务日志和 TUN 权限")
        raise policy.Deferred("透明代理规则或服务检测入口在 30 秒内未就绪；请检查 sing-box 服务日志")
    try:
        info = test_exit_ip(18081, protocol)
    except RuntimeError:
        recent = subprocess.run(['journalctl','-u','sing-box','--since','-20 seconds','-n','100','--no-pager'],capture_output=True,text=True,timeout=5)
        restriction = upstream_restriction(recent.stdout)
        if restriction:
            raise restriction
        raise
    key = os.environ.get('SBM_NODE_KEY')
    if not key:
        profile = pathlib.Path('/etc/sing-box/manager') / (protocol + '.json')
        key = policy.node_key(json.loads(profile.read_text()))
    policy.cache_put(key, info, active=True)
    ip = info.get("ip")
    EXIT_IP_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if ip:
        EXIT_IP_FILE.write_text(ip + "\n")
        EXIT_IP_FILE.chmod(0o600)
    else:
        EXIT_IP_FILE.unlink(missing_ok=True)
    fd, temporary = tempfile.mkstemp(dir=RESULT_FILE.parent, prefix=".probe-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(dict(info, protocol=protocol), stream)
        os.replace(temporary, RESULT_FILE)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    say(f"服务节点检测通过：{protocol} HTTPS 连通，出口 IP：{ip or '暂未查询到'}")
    return ip


def main():
    if len(sys.argv)==4 and sys.argv[1]=='--preflight':
        key=os.environ.get('SBM_NODE_KEY') or policy.node_key(json.loads(pathlib.Path(sys.argv[3]).read_text()))
        if policy.cache_get(key):
            say('近期已验证同一配置，复用结果；启动后仍验证实际连接。')
            return
        node = json.loads(pathlib.Path(sys.argv[3]).read_text())
        current_file = pathlib.Path('/etc/sing-box/manager') / (node['protocol'] + '.json')
        current = None
        try:
            current = json.loads(current_file.read_text())
        except (OSError, ValueError):
            pass
        same = current is not None and (policy.node_key(current) == key or (node['protocol'] == 'wg' and all(current.get(k) == node.get(k) for k in ('private_key','peer_public_key','peer_address','peer_port'))))
        # Reuse a running WireGuard peer, including mode changes, to avoid
        # creating two tunnels with the same key and stealing its endpoint.
        if same and pathlib.Path('/var/lib/singbox-manager/active').exists() and pathlib.Path('/var/lib/singbox-manager/active').read_text().strip() == node['protocol'] and subprocess.run(
                ['systemctl','is-active','--quiet','sing-box'], timeout=5).returncode == 0:
            info = test_exit_ip(18081, node['protocol'], include_ip=False)
        else:
            info=probe(sys.argv[2],inspector=lambda port:test_exit_ip(port,'node',include_ip=False),quiet=True)
        policy.cache_put(key,info)
    elif len(sys.argv)==3 and sys.argv[1]=='--active':
        if os.environ.get('SBM_POLICY_MANAGED')=='1':
            check_active(sys.argv[2])
        else:
            with policy.operation('current'):check_active(sys.argv[2])
    elif len(sys.argv)==2:
        with policy.operation('connect',wait=True):probe(sys.argv[1])
    else:
        raise ValueError('用法：probe.py --preflight CONFIG NODE | --active PROTOCOL | CONFIG')


if __name__ == "__main__":
    from utf8_runtime import ensure_utf8
    ensure_utf8()
    try:
        main()
    except policy.Deferred as exc:
        if isinstance(exc,policy.Restricted):
            try:policy.rpc({'op':'cooldown','reason':str(exc),'seconds':int(exc.retry_at-time.time())})
            except (policy.Deferred,OSError):pass
        print(f"检测延后：{exc}",file=sys.stderr)
        sys.exit(75)
    except OSError as exc:
        print('本机检测文件或工具暂不可用：' + type(exc).__name__,file=sys.stderr)
        sys.exit(75)
    except (ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"节点测试失败：{exc}",file=sys.stderr)
        sys.exit(1)
