#!/usr/bin/env python3
"""Prompt for one node profile without echoing passwords or private keys."""

import getpass
import json
import os
import pathlib
import sys
import tempfile

FIELDS = {
    "socks5": [
        ("server", "SOCKS5 服务器域名或 IP", False, False),
        ("server_port", "SOCKS5 端口", False, True),
        ("username", "用户名（可留空，输入 - 清空）", False, False),
        ("password", "密码（可留空，输入 - 清空）", True, False),
        ("udp_enabled", "上游是否支持 UDP 转发（y/n，默认 n）", False, False),
        ("direct_dns", "直连 DNS IP", False, False),
    ],
    "https": [
        ("server", "代理服务器域名或 IP", False, False),
        ("server_port", "代理端口", False, True),
        ("tls_server_name", "TLS 证书域名", False, False),
        ("username", "用户名", False, False),
        ("password", "密码", True, False),
        ("direct_dns", "直连 DNS IP", False, False),
    ],
    "shadowsocks": [
        ("server", "服务器域名或 IP", False, False),
        ("server_port", "服务器端口", False, True),
        ("method", "加密方法", False, False),
        ("password", "密码", True, False),
        ("direct_dns", "直连 DNS IP", False, False),
    ],
    "wg": [
        ("local_address", "本地地址/CIDR", False, False),
        ("private_key", "本机私钥", True, False),
        ("peer_address", "对端域名或 IP", False, False),
        ("peer_port", "对端端口", False, True),
        ("peer_public_key", "对端公钥", False, False),
        ("mtu", "MTU（必填，建议按服务商参数）", False, True),
        ("dns_server", "隧道内 DNS IP", False, False),
        ("direct_dns", "直连 DNS IP", False, False),
    ],
}


def main():
    protocol, filename = sys.argv[1:3]
    if protocol not in FIELDS:
        raise ValueError("未知协议")
    path = pathlib.Path(filename)
    node = json.loads(path.read_text())
    node["protocol"] = protocol
    print("直接回车保留当前值；密码和私钥不会回显。")
    if protocol == "socks5":
        print("无需认证时用户名和密码均留空。UDP 仅在上游支持 UDP ASSOCIATE 时开启。")
    for key, label, secret, numeric in FIELDS[protocol]:
        current = node.get(key, "")
        shown = "已填写" if secret and current and "REPLACE_ME" not in str(current) else str(current)
        if key == "udp_enabled":
            shown = "y" if current else "n"
        prompt = f"{label} [{shown}]: "
        entered = getpass.getpass(prompt) if secret else input(prompt)
        if not entered:
            continue
        if protocol == "socks5" and key in ("username", "password"):
            node[key] = "" if entered == "-" else entered
        elif key == "udp_enabled":
            if entered.lower() not in ("y", "yes", "true", "1", "n", "no", "false", "0"):
                print("请填写 y 或 n；此项保留原值。")
                continue
            node[key] = entered.lower() in ("y", "yes", "true", "1")
        elif numeric:
            if not entered.isdecimal():
                print(f"{label} 必须是数字；此项保留原值。")
                continue
            node[key] = int(entered)
        else:
            node[key] = entered.strip()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".node-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(node, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"已保存 {path}；从菜单选择对应启用项以校验并启用。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError, IndexError) as exc:
        print(f"编辑失败：{exc}", file=sys.stderr)
        sys.exit(1)
