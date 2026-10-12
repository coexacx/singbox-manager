"""HTTP health checks are independent of geolocation API availability."""

import ipaddress
import json
import re
import subprocess
import time

from ip_lookup import lookup_proxy_ip

HEALTH_URLS = ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204")
COUNTRIES = dict(zip(
    "US JP SG HK TW CN KR DE GB FR NL CA AU IN RU CH SE NO FI DK IT ES BR ZA AE TR PL AT BE IE ID MY TH VN PH NZ MX".split(),
    "美国 日本 新加坡 中国香港 中国台湾 中国 韩国 德国 英国 法国 荷兰 加拿大 澳大利亚 印度 俄罗斯 瑞士 瑞典 挪威 芬兰 丹麦 意大利 西班牙 巴西 南非 阿联酋 土耳其 波兰 奥地利 比利时 爱尔兰 印度尼西亚 马来西亚 泰国 越南 菲律宾 新西兰 墨西哥".split()))
CITIES = {"Tokyo": "东京", "Osaka": "大阪", "Singapore": "新加坡", "Hong Kong": "香港",
          "Los Angeles": "洛杉矶", "New York": "纽约", "London": "伦敦", "Frankfurt am Main": "法兰克福",
          "Amsterdam": "阿姆斯特丹", "Paris": "巴黎", "Taipei": "台北", "Seoul": "首尔"}


def request(port, url, max_time=10):
    result = subprocess.run([
        "curl", "--silent", "--show-error", "--noproxy", "",
        "--proxy", f"socks5h://127.0.0.1:{port}", "--connect-timeout", "5",
        "--max-time", str(max_time), "--write-out", "\n%{http_code} %{time_total}", url,
    ], capture_output=True, text=True, timeout=max_time + 3)
    body, _, trailer = result.stdout.rpartition("\n")
    fields = trailer.split()
    if result.returncode or len(fields) != 2:
        raise RuntimeError("代理 HTTP 连接失败")
    return body, int(fields[0]), round(float(fields[1]) * 1000, 1)


def health(port, attempts=3):
    for attempt in range(attempts):
        for url in HEALTH_URLS:
            try:
                _, status, latency = request(port, url)
                if status == 204:
                    return {"latency_ms": latency, "latency_url": url}
            except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        if attempt + 1 < attempts:
            time.sleep(1)
    raise RuntimeError("连续三次 HTTP 检测失败（已使用两个独立检测站点）")


def chinese_location(port, info):
    ip = str(ipaddress.ip_address(info["ip"]))
    # Look up the measured exit IP, not the server's direct IP. A 429 must not
    # turn a working node into a dead node.
    for url in (f"https://ipwho.is/{ip}?lang=zh-CN", f"http://ip-api.com/json/{ip}?lang=zh-CN"):
        try:
            body, status, _ = request(port, url)
            data = json.loads(body)
            if status != 200 or data.get("success") is False or data.get("status") == "fail":
                continue
            country, city = str(data.get("country", "")), str(data.get("city", ""))
            code = data.get("country_code") or data.get("countryCode") or ""
            country = country if re.search(r"[\u3400-\u9fff]", country) else COUNTRIES.get(code, "")
            city = city if re.search(r"[\u3400-\u9fff]", city) else CITIES.get(city, "")
            if country:
                return "-".join(dict.fromkeys(filter(None, (country, city))))
        except (ValueError, KeyError, OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    country = COUNTRIES.get(info.get("country"), "")
    city = CITIES.get(info.get("city"), "")
    return "-".join(filter(None, (country, city))) or "位置待查询"


def inspect(port, geography=False):
    info = health(port)
    if geography:
        try:
            info.update(lookup_proxy_ip(port, rounds=1))
            info["location_zh"] = chinese_location(port, info)
        except (ValueError, OSError, RuntimeError):
            info["location_zh"] = "位置待查询"
    return info
