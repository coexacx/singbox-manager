"""HTTP health checks are independent of geolocation API availability."""

import ipaddress
import json
import re
import subprocess
import time

from ip_lookup import lookup_proxy_ip
from probe_policy import Deferred, Restricted

HEALTH_URLS = ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204")
COUNTRIES = dict(zip(
    "US JP SG HK TW CN KR DE GB FR NL CA AU IN RU CH SE NO FI DK IT ES BR ZA AE TR PL AT BE IE ID MY TH VN PH NZ MX".split(),
    "美国 日本 新加坡 中国香港 中国台湾 中国 韩国 德国 英国 法国 荷兰 加拿大 澳大利亚 印度 俄罗斯 瑞士 瑞典 挪威 芬兰 丹麦 意大利 西班牙 巴西 南非 阿联酋 土耳其 波兰 奥地利 比利时 爱尔兰 印度尼西亚 马来西亚 泰国 越南 菲律宾 新西兰 墨西哥".split()))
COUNTRIES.update(dict(zip(
    'AD AL AR AM AZ BA BD BG BH BO BY CL CO CR CY CZ DO EC EE EG GE GR GT HR HU IL IS KE KH KZ LA LK LT LU LV MA MD ME MK MT NG NP OM PA PE PK PR PT QA RO RS SA SI SK TN UA UY UZ VE'.split(),
    '安道尔 阿尔巴尼亚 阿根廷 亚美尼亚 阿塞拜疆 波黑 孟加拉国 保加利亚 巴林 玻利维亚 白俄罗斯 智利 哥伦比亚 哥斯达黎加 塞浦路斯 捷克 多米尼加 厄瓜多尔 爱沙尼亚 埃及 格鲁吉亚 希腊 危地马拉 克罗地亚 匈牙利 以色列 冰岛 肯尼亚 柬埔寨 哈萨克斯坦 老挝 斯里兰卡 立陶宛 卢森堡 拉脱维亚 摩洛哥 摩尔多瓦 黑山 北马其顿 马耳他 尼日利亚 尼泊尔 阿曼 巴拿马 秘鲁 巴基斯坦 波多黎各 葡萄牙 卡塔尔 罗马尼亚 塞尔维亚 沙特阿拉伯 斯洛文尼亚 斯洛伐克 突尼斯 乌克兰 乌拉圭 乌兹别克斯坦 委内瑞拉'.split())))
CITIES = {"Tokyo": "东京", "Osaka": "大阪", "Singapore": "新加坡", "Hong Kong": "香港",
          "Los Angeles": "洛杉矶", "New York": "纽约", "London": "伦敦", "Frankfurt am Main": "法兰克福",
          "Amsterdam": "阿姆斯特丹", "Paris": "巴黎", "Taipei": "台北", "Seoul": "首尔"}


def request(port, url, max_time=5):
    result = subprocess.run([
        "curl", "--silent", "--show-error", "--noproxy", "",
        "--proxy", f"socks5h://127.0.0.1:{port}", "--connect-timeout", "5",
        "--max-time", str(max_time), "--write-out", "\n%{http_code} %{time_total}", url,
    ], capture_output=True, text=True, timeout=max_time + 1)
    body, _, trailer = result.stdout.rpartition("\n")
    fields = trailer.split()
    if result.returncode or len(fields) != 2:
        raise RuntimeError("代理 HTTP 连接失败")
    return body, int(fields[0]), round(float(fields[1]) * 1000, 1)


def health(port, attempts=1, timeout=12, progress=None):
    deadline = time.monotonic() + timeout
    target_problem = False
    for attempt in range(attempts):
        for url in HEALTH_URLS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if target_problem:
                    raise Deferred('检测网站响应异常，稍后复核', time.time()+900, 'target')
                raise RuntimeError("代理 HTTPS 连通性检测超时")
            if progress:
                progress(f"HTTPS 检测 {attempt + 1}/{attempts}：{url.split('/')[2]}")
            try:
                _, status, latency = request(port, url, max_time=min(5, remaining))
                if status == 204:
                    return {"latency_ms": latency, "latency_url": url}
                target_problem = True
            except Restricted:
                raise
            except (ValueError, OSError):
                raise Deferred('本机 HTTPS 检测工具或响应解析异常，保留节点', time.time()+180, 'local') from None
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        if attempt + 1 < attempts:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    if target_problem:
        raise Deferred("检测网站返回限流或非预期响应，暂不判定节点死亡", time.time()+900, 'target')
    raise RuntimeError("代理 HTTPS 请求失败（已用备用地址复核）")


def chinese_location(port, info, timeout=10, online=False):
    ip = str(ipaddress.ip_address(info["ip"]))
    deadline = time.monotonic() + timeout
    # Look up the measured exit IP, not the server's direct IP. A 429 must not
    # turn a working node into a dead node.
    for url in ((f"https://ipwho.is/{ip}?lang=zh-CN", f"http://ip-api.com/json/{ip}?lang=zh-CN") if online else ()):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            body, status, _ = request(port, url, max_time=min(5, remaining))
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
    country = COUNTRIES.get(info.get("country_code") or info.get("country"), "")
    city = CITIES.get(info.get("city"), "")
    return "-".join(dict.fromkeys(filter(None, (country, city)))) or "位置待查询"


def inspect(port, geography=False):
    info = health(port)
    if geography:
        try:
            info.update(lookup_proxy_ip(port, rounds=1, timeout=8, max_sources=1))
            info["location_zh"] = chinese_location(port, info)
        except (ValueError, OSError, RuntimeError):
            info["location_zh"] = "位置待查询"
    return info
