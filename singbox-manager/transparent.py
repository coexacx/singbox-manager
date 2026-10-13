#!/usr/bin/env python3
"""Own the nftables and policy routes used when the TUN inbound is disabled."""

import json
import os
import pathlib
import pwd
import subprocess
import sys

CONFIG = pathlib.Path("/etc/sing-box/config.json")
TABLE = "singbox_manager"
ROUTE_TABLE = "18082"
RULE_PRIORITY = "18082"
MARK = "0x4001"


def run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, text=True, capture_output=True, check=check)


def cleanup():
    run("nft", "delete", "table", "inet", TABLE, check=False)
    for family in ("-4", "-6"):
        run("ip", family, "rule", "del", "pref", RULE_PRIORITY,
            "fwmark", f"{MARK}/{MARK}", "lookup", ROUTE_TABLE, check=False)
        run("ip", family, "route", "flush", "table", ROUTE_TABLE, check=False)


def rule_text(config):
    uid = pwd.getpwnam("sing-box").pw_uid
    ipv6 = any(item.get("tag") == "tproxy6-in" for item in config["inbounds"])
    ports = set()
    for rule in config["route"]["rules"]:
        if rule.get("action") == "bypass":
            ports.update(port for port in rule.get("source_port", []) if isinstance(port, int) and 1 <= port <= 65535)
    ssh_rule = f"tcp sport {{ {', '.join(map(str, sorted(ports)))} }} return" if ports else ""
    ipv6_rule = "meta mark 0x4001 meta nfproto ipv6 meta l4proto { tcp, udp } tproxy ip6 to :18083 accept" if ipv6 else ""
    ipv6_input = "iifname != \"lo\" meta nfproto ipv6 meta l4proto { tcp, udp } th dport 18083 drop" if ipv6 else ""
    return f"""table inet {TABLE} {{
      chain output {{
        type route hook output priority mangle; policy accept;
        meta mark 0x4002 return
        meta skuid {uid} return
        oifname \"lo\" return
        ip daddr 0.0.0.0/8 return
        ip daddr 224.0.0.0/4 return
        ip daddr 255.255.255.255 return
        ip6 daddr ::1/128 return
        ip6 daddr fe80::/10 return
        ip6 daddr ff00::/8 return
        {ssh_rule}
        meta l4proto {{ tcp, udp }} meta mark set {MARK}
      }}
      chain prerouting {{
        type filter hook prerouting priority mangle; policy accept;
        iifname != \"lo\" return
        meta mark {MARK} meta nfproto ipv4 meta l4proto {{ tcp, udp }} tproxy ip to :18082 accept
        {ipv6_rule}
      }}
      chain input {{
        type filter hook input priority filter; policy accept;
        iifname != \"lo\" meta l4proto {{ tcp, udp }} th dport 18082 drop
        {ipv6_input}
      }}
    }}
    """


def sync():
    cleanup()
    config = json.loads(CONFIG.read_text())
    if not any(item.get("type") == "tproxy" for item in config["inbounds"]):
        return
    ipv6 = any(item.get("tag") == "tproxy6-in" for item in config["inbounds"])
    rules = rule_text(config)
    try:
        run("nft", "-c", "-f", "-", input_text=rules)
        run("ip", "-4", "route", "replace", "local", "0.0.0.0/0", "dev", "lo", "table", ROUTE_TABLE)
        run("ip", "-4", "rule", "add", "pref", RULE_PRIORITY, "fwmark", f"{MARK}/{MARK}", "lookup", ROUTE_TABLE)
        if ipv6:
            run("ip", "-6", "route", "replace", "local", "::/0", "dev", "lo", "table", ROUTE_TABLE)
            run("ip", "-6", "rule", "add", "pref", RULE_PRIORITY, "fwmark", f"{MARK}/{MARK}", "lookup", ROUTE_TABLE)
        run("nft", "-f", "-", input_text=rules)
    except (OSError, subprocess.CalledProcessError):
        cleanup()
        raise


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("sync", "stop"):
            raise ValueError("用法：transparent.py sync|stop")
        if sys.argv[1] == "sync":
            sync()
        else:
            cleanup()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print(f"透明代理规则错误：{exc.stderr.strip()[:500]}", file=sys.stderr)
        else:
            print(f"透明代理规则错误：{exc}", file=sys.stderr)
        sys.exit(1)
