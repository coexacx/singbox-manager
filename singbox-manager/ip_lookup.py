#!/usr/bin/env python3
"""Query independent IP services through sing-box's local SOCKS listener."""

import ipaddress
import json
import subprocess
import time


SOURCES = (
    ("ipinfo.io", "https://ipinfo.io/json"),
    ("ipwho.is", "https://ipwho.is/"),
    ("api.ipify.org", "https://api.ipify.org?format=json"),
)


def lookup_proxy_ip(port, rounds=2):
    """Return the first valid proxy exit IP and any available location data."""
    errors = []
    for attempt in range(rounds):
        for name, url in SOURCES:
            result = subprocess.run([
                "curl", "--silent", "--show-error", "--location", "--noproxy", "",
                "--proxy", f"socks5h://127.0.0.1:{port}",
                "--connect-timeout", "6", "--max-time", "14",
                "--write-out", "\n%{http_code}", url,
            ], capture_output=True, text=True)
            body, _, status = result.stdout.rpartition("\n")
            if result.returncode or status != "200":
                detail = result.stderr.strip()[:100] if result.returncode else f"HTTP {status or '未知'}"
                errors.append(f"{name}: {detail}")
                continue
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or data.get("success") is False:
                    raise ValueError("响应无效")
                ip = str(ipaddress.ip_address(data["ip"]))
                connection = data.get("connection") or {}
                if not isinstance(connection, dict):
                    connection = {}
                return {
                    "ip": ip,
                    "city": str(data.get("city") or ""),
                    "region": str(data.get("region") or ""),
                    "country": str(data.get("country") or ""),
                    "org": str(data.get("org") or connection.get("isp") or ""),
                    "source": name,
                }
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                errors.append(f"{name}: IP 数据无效")
        if attempt + 1 < rounds:
            time.sleep(2)
    raise RuntimeError("所有代理出口 IP 查询服务均失败：" + "；".join(errors[-len(SOURCES):]))
