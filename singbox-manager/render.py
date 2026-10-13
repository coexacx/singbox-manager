#!/usr/bin/env python3
"""Render one of the supplied rule templates as a machine-wide sing-box config."""

import base64
import ipaddress
import json
import os
import pathlib
import subprocess
import sys


def fail(message):
    raise ValueError(message)


def value(node, key):
    result = node.get(key)
    if not isinstance(result, str) or not result.strip() or "REPLACE_ME" in result:
        fail(f"请填写 {key}")
    return result.strip()


def port(node, key):
    result = node.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or not 1 <= result <= 65535:
        fail(f"{key} 必须是 1–65535 的端口数字")
    return result


def wg_key(node, key):
    result = value(node, key)
    try:
        if len(base64.b64decode(result, validate=True)) != 32:
            fail(f"{key} 必须是 32 字节 WireGuard 密钥")
    except (ValueError, base64.binascii.Error):
        fail(f"{key} 必须是 Base64 格式的 WireGuard 密钥")
    return result


def has_ipv6_default():
    try:
        result = subprocess.run(
            ["ip", "-6", "route", "show", "default"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return False


def ssh_peer_routes():
    """Keep existing administrative SSH sessions outside the machine TUN."""
    peers = set()
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if connection:
        peers.add(connection[0])
    client = os.environ.get("SSH_CLIENT", "").split()
    if client:
        peers.add(client[0])
    try:
        result = subprocess.run(
            ["ss", "-Htnp", "state", "established"],
            capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            if '"sshd' not in line and '"dropbear"' not in line:
                continue
            columns = line.split()
            if len(columns) >= 4:
                host = columns[3].rsplit(":", 1)[0].strip("[]")
                peers.add(host)
    except (OSError, subprocess.CalledProcessError):
        pass
    routes = []
    for peer in sorted(peers):
        try:
            address = ipaddress.ip_address(peer.split("%", 1)[0])
        except ValueError:
            continue
        if not address.is_loopback:
            routes.append(f"{address}/{address.max_prefixlen}")
    return routes


def ssh_server_ports():
    ports = set()
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) == 4 and connection[3].isdigit():
        ports.add(int(connection[3]))
    try:
        result = subprocess.run(
            ["ss", "-Hltnp"], capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            if '"sshd"' not in line and '"dropbear"' not in line:
                continue
            columns = line.split()
            if len(columns) >= 4:
                raw = columns[3].rsplit(":", 1)[-1]
                if raw.isdigit():
                    ports.add(int(raw))
    except (OSError, subprocess.CalledProcessError):
        pass
    return sorted(port for port in ports if 1 <= port <= 65535)


def render(node, template, tun_mode="on", traffic_mode="split"):
    protocol = node.get("protocol")
    if protocol not in ("https", "shadowsocks", "wg", "socks5"):
        fail("protocol 只能为 https、shadowsocks、wg 或 socks5")
    if tun_mode not in ("on", "off"):
        fail("TUN 模式只能为 on 或 off")
    if traffic_mode not in ("split", "global"):
        fail("流量模式只能为 split 或 global")

    config = json.loads(template.read_text())
    config["log"] = {"level": "info", "timestamp": True}
    address = ["172.31.255.1/30"]
    if has_ipv6_default():
        address.append("fdfe:dcba:9876::1/126")
    if tun_mode == "on":
        ingress = [{
            "type": "tun", "tag": "tun-in", "interface_name": "sb-split0",
            "address": address, "auto_route": True, "auto_redirect": True,
            "strict_route": False, "dns_mode": "hijack",
            "route_exclude_address": ssh_peer_routes(),
        }]
    else:
        ingress = [{
            "type": "tproxy", "tag": "tproxy-in", "listen": "0.0.0.0",
            "listen_port": 18082,
        }]
        if has_ipv6_default():
            ingress.append({
                "type": "tproxy", "tag": "tproxy6-in", "listen": "::",
                "listen_port": 18083,
            })
    config["inbounds"] = ingress + [{
        "type": "socks", "tag": "health-in", "listen": "127.0.0.1",
        "listen_port": 18081,
    }]
    config["route"]["auto_detect_interface"] = True
    config["route"]["final"] = "direct"
    config.setdefault("outbounds", []).append({"type": "direct", "tag": "direct"})
    ingress_tags = [item["tag"] for item in ingress]
    config["route"]["rules"].insert(0, {
        "inbound": ingress_tags, "port": 53, "action": "hijack-dns"
    })
    if tun_mode == "off":
        config["route"]["rules"].insert(1, {
            "inbound": ingress_tags, "action": "sniff"
        })
    ssh_ports = ssh_server_ports()
    if ssh_ports:
        config["route"]["rules"].insert(0, {
            "network": "tcp", "source_port": ssh_ports,
            "action": "bypass", "outbound": "direct",
        })
    for rule in config["route"]["rules"]:
        if rule.get("inbound") == ["anytls-in"]:
            rule["inbound"] = ingress_tags
    for rule_set in config["route"]["rule_set"]:
        if rule_set.get("type") == "remote":
            rule_set.pop("download_detour", None)
            rule_set["http_client"] = {"engine": "go"}
    tags = [item["tag"] for item in config["route"]["rule_set"]]
    config["experimental"]["cache_file"]["path"] = "/var/lib/sing-box/split-fakeip-cache.db"
    config["experimental"]["cache_file"]["store_fakeip"] = True
    direct_dns = value(node, "direct_dns")
    ipaddress.ip_address(direct_dns)

    if protocol == "https":
        outbound = config["outbounds"][0]
        outbound["server"] = value(node, "server")
        outbound["server_port"] = port(node, "server_port")
        outbound["username"] = value(node, "username")
        outbound["password"] = value(node, "password")
        outbound["tls"]["server_name"] = value(node, "tls_server_name")
    elif protocol == "shadowsocks":
        outbound = config["outbounds"][0]
        outbound["server"] = value(node, "server")
        outbound["server_port"] = port(node, "server_port")
        outbound["method"] = value(node, "method")
        outbound["password"] = value(node, "password")
    elif protocol == "socks5":
        outbound = config["outbounds"][0]
        outbound["server"] = value(node, "server")
        outbound["server_port"] = port(node, "server_port")
        outbound["version"] = "5"
        username, password = node.get("username", ""), node.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            fail("SOCKS5 用户名和密码必须是字符串；无需认证时两项均留空")
        if bool(username) != bool(password):
            fail("SOCKS5 用户名和密码必须同时填写，或同时留空")
        for key, credential in (("username", username), ("password", password)):
            if credential:
                if "REPLACE_ME" in credential or len(credential.encode("utf-8")) > 255:
                    fail(f"SOCKS5 {key} 必须填写有效值，且 UTF-8 编码不超过 255 字节")
                outbound[key] = credential
            else:
                outbound.pop(key, None)
        udp_enabled = node.get("udp_enabled", False)
        if not isinstance(udp_enabled, bool):
            fail("SOCKS5 udp_enabled 必须为 true 或 false")
        if udp_enabled:
            outbound.pop("network", None)
            config["route"]["rules"] = [
                rule for rule in config["route"]["rules"]
                if not (rule.get("action") == "reject" and rule.get("network") == "udp"
                        and rule.get("inbound") == ingress_tags and rule.get("rule_set"))
            ]
        else:
            outbound["network"] = "tcp"
    else:
        endpoint = config["endpoints"][0]
        peer = endpoint["peers"][0]
        mtu = node.get("mtu")
        if not isinstance(mtu, int) or isinstance(mtu, bool) or not 576 <= mtu <= 9000:
            fail("启用 WireGuard 前必须填写 576–9000 的 MTU")
        endpoint["mtu"] = mtu
        local_address = value(node, "local_address")
        ipaddress.ip_interface(local_address)
        endpoint["address"] = [local_address]
        endpoint["private_key"] = wg_key(node, "private_key")
        peer["address"] = value(node, "peer_address")
        peer["port"] = port(node, "peer_port")
        peer["public_key"] = wg_key(node, "peer_public_key")
        primary_dns = value(node, "dns_server")
        ipaddress.ip_address(primary_dns)
        # The source template's local DNS can recurse after TUN takes the default route.
        for server in config["dns"]["servers"]:
            if server["tag"] == "surfshark-bootstrap":
                server.clear()
                server.update({"type": "udp", "tag": "surfshark-bootstrap", "server": direct_dns})
            elif server["tag"] == "surfshark-dns-primary":
                server["server"] = primary_dns
        # Keep previous FakeIP mappings available when switching from HTTP/SS.
        config["dns"]["servers"].append({
            "type": "fakeip", "tag": "target-fakeip", "inet4_range": "198.18.0.0/15"
        })
        config["dns"]["rules"] = [{
            "rule_set": tags, "action": "route", "server": "surfshark-dns-primary"
        }]

    if protocol in ("https", "shadowsocks", "socks5"):
        # FakeIP keeps selected hostnames intact for HTTP CONNECT and remote proxy DNS.
        config["dns"] = {
            "servers": [
                {"type": "udp", "tag": "dns-direct", "server": direct_dns},
                {"type": "fakeip", "tag": "target-fakeip", "inet4_range": "198.18.0.0/15"},
            ],
            "rules": [{"rule_set": tags, "action": "route", "server": "target-fakeip"}],
            "final": "dns-direct", "strategy": "ipv4_only",
        }
        config["route"]["default_domain_resolver"] = "dns-direct"
    selected = {"https": "https-out", "shadowsocks": "shadowsocks-out", "wg": "surfshark-wg", "socks5": "socks5-out"}[protocol]
    health_rules = []
    if protocol == "wg":
        health_rules.append({
            "inbound": ["health-in"], "action": "resolve",
            "server": "surfshark-dns-primary", "strategy": "ipv4_only",
        })
    health_rules.append({"inbound": ["health-in"], "action": "route", "outbound": selected})
    config["route"]["rules"] = health_rules + config["route"]["rules"]
    if traffic_mode == "global":
        config["route"]["final"] = selected
        if protocol == "wg":
            config["dns"]["rules"] = []
            config["dns"]["final"] = "surfshark-dns-primary"
        else:
            config["dns"]["rules"] = [
                # The alert sender binds to the physical NIC and cannot reach FakeIP.
                {"domain": ["api.telegram.org"], "action": "route", "server": "dns-direct"},
                {"query_type": ["A", "AAAA"], "action": "route", "server": "target-fakeip"},
            ]
        # A TCP-only upstream cannot carry UDP for any destination in global mode.
        for rule in config["route"]["rules"]:
            if rule.get("action") == "reject" and rule.get("network") == "udp" and rule.get("inbound") == ingress_tags:
                rule.pop("rule_set", None)
        config["route"]["rules"] = [rule for rule in config["route"]["rules"] if not rule.get("rule_set")]
        config["route"]["rule_set"] = []
    return config


def main():
    if len(sys.argv) not in (4, 5, 6):
        fail("用法：render.py NODE.json TEMPLATES_DIR OUTPUT.json [on|off] [split|global]")
    node = json.loads(pathlib.Path(sys.argv[1]).read_text())
    protocol = node.get("protocol")
    if protocol not in ("https", "shadowsocks", "wg", "socks5"):
        fail("protocol 只能为 https、shadowsocks、wg 或 socks5")
    template = pathlib.Path(sys.argv[2]) / (protocol + ".json")
    config = render(node, template, sys.argv[4] if len(sys.argv) >= 5 else "on",
                    sys.argv[5] if len(sys.argv) == 6 else "split")
    pathlib.Path(sys.argv[3]).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        sys.exit(2)
