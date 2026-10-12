"""Convert numbered WireGuard configs, proxy URIs and sing-box objects."""

import base64
import configparser
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit


def decode64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


def csv(text):
    return [item.strip() for item in text.split(",") if item.strip()]


def endpoint(text):
    parsed = urlsplit("//" + text)
    if not parsed.hostname or not parsed.port:
        raise ValueError("服务器必须填写 host:port；IPv6 使用 [地址]:端口")
    return parsed.hostname, parsed.port


def normalize(node):
    node = dict(node)
    node.setdefault("direct_dns", "1.1.1.1")
    if node.get("protocol") == "wg":
        node.setdefault("mtu", 1280)
        node.setdefault("dns_server", "1.1.1.1")
        if isinstance(node.get("local_address"), str):
            node["local_address"] = csv(node["local_address"])
        node.setdefault("allowed_ips", ["0.0.0.0/0", "::/0"])
        node.setdefault("persistent_keepalive", 25)
    if node.get("protocol") in ("https", "socks5"):
        node.setdefault("username", "")
        node.setdefault("password", "")
    if node.get("protocol") == "socks5":
        node.setdefault("udp_enabled", False)
    return node


def wireguard(text):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(text)
    interface, peer = parser["Interface"], parser["Peer"]
    host, port = endpoint(peer["Endpoint"])
    dns = csv(interface.get("DNS", "1.1.1.1"))
    node = {
        "protocol": "wg", "private_key": interface["PrivateKey"].strip(),
        "local_address": csv(interface["Address"]),
        "mtu": int(interface.get("MTU", "1280")),
        "dns_server": dns[0] if dns else "1.1.1.1",
        "peer_address": host, "peer_port": port,
        "peer_public_key": peer["PublicKey"].strip(),
        "allowed_ips": csv(peer.get("AllowedIPs", "0.0.0.0/0,::/0")),
        "persistent_keepalive": int(peer.get("PersistentKeepalive", "25")),
    }
    if peer.get("PresharedKey"):
        node["pre_shared_key"] = peer["PresharedKey"].strip()
    return normalize(node)


def proxy_uri(text):
    if "://" not in text:
        text = "socks5://" + text
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    query = parse_qs(parsed.query)
    if scheme == "ss":
        if query.get("plugin"):
            raise ValueError("暂不支持带 plugin 的 Shadowsocks 链接")
        authority = text.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if "@" not in authority:
            authority = decode64(authority or parsed.path)
        credentials, server = authority.rsplit("@", 1)
        credentials = unquote(credentials)
        if ":" not in credentials:
            credentials = decode64(credentials)
        method, password = credentials.split(":", 1)
        host, port = endpoint(server)
        return normalize({"protocol": "shadowsocks", "server": host,
                          "server_port": port, "method": method, "password": password})
    if scheme in ("wireguard", "wg"):
        host = parsed.hostname
        node = {"protocol": "wg", "peer_address": host, "peer_port": parsed.port or 51820,
                "private_key": unquote(parsed.username or ""),
                "peer_public_key": query.get("publickey", query.get("public_key", [""]))[0],
                "local_address": csv(query.get("address", [""])[0]),
                "dns_server": csv(query.get("dns", ["1.1.1.1"])[0])[0],
                "mtu": int(query.get("mtu", ["1280"])[0])}
        if query.get("presharedkey"):
            node["pre_shared_key"] = query["presharedkey"][0]
        return normalize(node)
    if scheme not in ("https", "socks", "socks5", "socks5h"):
        raise ValueError("支持 HTTPS、SOCKS5、ss://、WireGuard 配置或 wg:// 链接")
    protocol = "https" if scheme == "https" else "socks5"
    if not parsed.hostname:
        raise ValueError("节点缺少服务器地址")
    node = {"protocol": protocol, "server": parsed.hostname,
            "server_port": parsed.port or (443 if protocol == "https" else 1080),
            "username": unquote(parsed.username or ""), "password": unquote(parsed.password or "")}
    if protocol == "https":
        node["tls_server_name"] = query.get("sni", [parsed.hostname])[0]
    return normalize(node)


def singbox_object(data):
    if not isinstance(data, dict):
        raise ValueError("每个编号对应一个节点对象")
    if "protocol" in data:
        return normalize(data)
    if "outbounds" in data or "endpoints" in data:
        choices = [x for x in data.get("outbounds", []) + data.get("endpoints", [])
                   if x.get("type") in ("http", "socks", "shadowsocks", "wireguard")]
        if len(choices) != 1:
            raise ValueError("每个编号的 JSON 必须恰好包含一个代理节点")
        data = choices[0]
    kind = data.get("type")
    if kind == "wireguard":
        peers = data.get("peers", [])
        if len(peers) != 1:
            raise ValueError("每个 WireGuard 节点须恰好包含一个 Peer")
        peer = peers[0]
        node = {"protocol": "wg", "private_key": data["private_key"],
                "local_address": data["address"], "mtu": data.get("mtu", 1280),
                "peer_address": peer["address"], "peer_port": peer["port"],
                "peer_public_key": peer["public_key"],
                "allowed_ips": peer.get("allowed_ips", ["0.0.0.0/0", "::/0"]),
                "persistent_keepalive": peer.get("persistent_keepalive_interval", 25)}
        if peer.get("pre_shared_key"):
            node["pre_shared_key"] = peer["pre_shared_key"]
        return normalize(node)
    if kind == "http":
        if not data.get("tls", {}).get("enabled"):
            raise ValueError("HTTPS 模板需要启用 TLS 的 HTTP 出站")
        if data["tls"].get("insecure"):
            raise ValueError("不支持关闭证书验证的 HTTPS 节点")
        node = {"protocol": "https", "tls_server_name": data["tls"].get("server_name", data["server"])}
    elif kind == "socks":
        if str(data.get("version", "5")) != "5":
            raise ValueError("仅支持 SOCKS5")
        node = {"protocol": "socks5", "udp_enabled": data.get("network") != "tcp"}
    elif kind == "shadowsocks":
        node = {"protocol": "shadowsocks", "method": data["method"]}
    else:
        raise ValueError("无法识别 JSON 节点协议")
    for key in ("server", "server_port", "username", "password"):
        if key in data:
            node[key] = data[key]
    return normalize(node)


def parse_block(text):
    text = text.strip()
    if "[Interface]" in text:
        return wireguard(text)
    if text.startswith("{"):
        return singbox_object(json.loads(text))
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", ";"))]
    if len(lines) != 1:
        raise ValueError("每个编号后只能放一个节点；请用独立数字行隔离")
    return proxy_uri(lines[0])


def parse_numbered(text):
    """Return (number, node, error) without leaking raw credential text on errors."""
    blocks, number, lines = [], None, []
    for line in text.lstrip("\ufeff").splitlines():
        match = re.fullmatch(r"\s*(\d+)\s*[.、:：)）]?\s*", line)
        if match:
            if number is not None or any(s.strip() and not s.lstrip().startswith(("#", ";")) for s in lines):
                blocks.append((number or "未编号", "\n".join(lines)))
            number, lines = match[1], []
        else:
            lines.append(line)
    if number is not None or any(s.strip() for s in lines):
        blocks.append((number or "1", "\n".join(lines)))
    result = []
    for number, block in blocks:
        try:
            result.append((number, parse_block(block), None))
        except (ValueError, KeyError, IndexError, configparser.Error):
            result.append((number, None, "格式无效或含不支持的字段，请检查节点格式、端口和编号分隔"))
    return result
