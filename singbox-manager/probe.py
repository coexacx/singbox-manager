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

from ip_lookup import lookup_proxy_ip

EXIT_IP_FILE = pathlib.Path("/var/lib/singbox-manager/exit_ip")


def test_exit_ip(port, protocol):
    return lookup_proxy_ip(port, rounds=3 if protocol == "wg" else 2)


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def physical_interface():
    for family in ("-4", "-6"):
        result = subprocess.run(["ip", family, "route", "show", "default", "table", "main"], capture_output=True, text=True)
        if result.returncode == 0:
            words = result.stdout.split()
            if "dev" in words:
                return words[words.index("dev") + 1]
    return None


def probe(config_path):
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
            "default_mark": "0x4002",
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
        checked = subprocess.run(["sing-box", "check", "-c", str(candidate)], capture_output=True, text=True)
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
                info = test_exit_ip(port, protocol)
                print(f"节点测试通过：经 {protocol} 节点访问 {info['source']}，出口 IP：{info['ip']}")
                return info["ip"]
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


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
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", 18081), timeout=0.2):
                pass
        except OSError:
            time.sleep(0.5)
            continue
        if "tun" in inbound_types:
            link = subprocess.run(["ip", "link", "show", tun_name], capture_output=True)
            ready = link.returncode == 0
        else:
            table = subprocess.run(["nft", "list", "table", "inet", "singbox_manager"], capture_output=True)
            rules = subprocess.run(["ip", "-4", "rule", "show"], capture_output=True, text=True)
            ready = (table.returncode == 0 and rules.returncode == 0
                     and "fwmark 0x4001/0x4001 lookup 18082" in rules.stdout)
        if ready:
            break
        time.sleep(0.5)
    if not ready:
        if "tun" in inbound_types:
            if not pathlib.Path("/dev/net/tun").exists():
                raise RuntimeError("TUN 设备 /dev/net/tun 不存在；请在服务器宿主机开放 TUN")
            raise RuntimeError(f"TUN 接口 {tun_name} 或服务检测入口在 60 秒内未就绪；请检查 sing-box 服务日志和 TUN 权限")
        raise RuntimeError("透明代理规则或服务检测入口在 60 秒内未就绪；请检查 sing-box 服务日志")
    info = test_exit_ip(18081, protocol)
    ip = info["ip"]
    EXIT_IP_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    EXIT_IP_FILE.write_text(ip + "\n")
    EXIT_IP_FILE.chmod(0o600)
    print(f"服务节点检测通过：经 {protocol} 节点访问 {info['source']}，出口 IP：{ip}")
    return ip


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2:
            probe(sys.argv[1])
        elif len(sys.argv) == 3 and sys.argv[1] == "--active":
            check_active(sys.argv[2])
        else:
            raise ValueError("用法：probe.py CONFIG.json 或 probe.py --active PROTOCOL")
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"节点测试失败：{exc}", file=sys.stderr)
        sys.exit(1)
