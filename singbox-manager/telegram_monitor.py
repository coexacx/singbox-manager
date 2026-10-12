#!/usr/bin/env python3
"""Telegram configuration and periodic sing-box status notifications."""

import getpass
import grp
import http.client
import json
import os
import pathlib
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

CONFIG = pathlib.Path("/etc/sing-box/manager/telegram.json")
SINGBOX_CONFIG = pathlib.Path("/etc/sing-box/config.json")
TOKEN_RE = re.compile(r"^[0-9]{5,}:[A-Za-z0-9_-]{20,}$")
USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
from ip_lookup import lookup_proxy_ip
from node_notifications import event_lines, summary_lines, split_message


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, check=False)


def physical_interface():
    for family in ("-4", "-6"):
        result = command("ip", "-j", family, "route", "show", "default", "table", "main")
        if result.returncode == 0:
            try:
                routes = json.loads(result.stdout)
                for route in routes:
                    if route.get("dev") and route.get("dev") != "lo":
                        return route["dev"]
            except json.JSONDecodeError:
                pass
    return None


def address_family():
    result = command("ip", "-4", "route", "show", "default", "table", "main")
    return socket.AF_INET if result.returncode == 0 and result.stdout.strip() else socket.AF_INET6


def local_ip(interface):
    if not interface:
        return "未知"
    for family in ("-4", "-6"):
        result = command("ip", "-j", family, "address", "show", "dev", interface)
        if result.returncode:
            continue
        try:
            for device in json.loads(result.stdout):
                for address in device.get("addr_info", []):
                    if address.get("scope") == "global" and address.get("local"):
                        return address["local"]
        except json.JSONDecodeError:
            continue
    return "未知"


class DirectHTTPSConnection(http.client.HTTPSConnection):
    """Bind Bot API traffic to the physical NIC so alerts survive proxy failure."""

    def connect(self):
        interface = physical_interface()
        if not interface:
            raise OSError("未找到物理默认网络接口")
        last_error = None
        for family, socktype, proto, _, address in socket.getaddrinfo(
            self.host, self.port, family=address_family(), type=socket.SOCK_STREAM
        ):
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, 0x4002)
                except OSError:
                    pass
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
                sock.close()
        raise OSError(f"Bot API 直连失败：{last_error}")


def bot_api(token, method, payload=None):
    body = urlencode(payload or {}).encode()
    connection = DirectHTTPSConnection("api.telegram.org", timeout=18, context=ssl.create_default_context())
    try:
        connection.request("POST", f"/bot{token}/{method}", body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
        response = connection.getresponse()
        raw = response.read(65536)
        data = json.loads(raw)
        if not data.get("ok"):
            raise ValueError(data.get("description", f"Telegram HTTP {response.status}"))
        return data["result"]
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise ValueError(f"Bot API 请求失败：{type(exc).__name__}") from exc
    finally:
        connection.close()


def load_config():
    if not CONFIG.is_file():
        return {}
    data = json.loads(CONFIG.read_text())
    return data if isinstance(data, dict) else {}


def save_config(data):
    CONFIG.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".telegram.", dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fchmod(file.fileno(), 0o640)
            os.fchown(file.fileno(), 0, grp.getgrnam("sing-box").gr_gid)
        os.replace(name, CONFIG)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def resolve_chat(token, recipient):
    if re.fullmatch(r"-?[0-9]+", recipient):
        chat = bot_api(token, "getChat", {"chat_id": recipient})
        return str(chat["id"]), chat.get("username", ""), chat.get("type", "")
    if not USERNAME_RE.fullmatch(recipient):
        raise ValueError("收件人请填写 @用户名或数字 chat_id")
    try:
        chat = bot_api(token, "getChat", {"chat_id": recipient})
        if chat.get("type") in ("channel", "supergroup", "group"):
            return str(chat["id"]), chat.get("username", ""), chat.get("type", "")
    except ValueError:
        pass
    webhook = bot_api(token, "getWebhookInfo")
    if webhook.get("url"):
        return None, None, "webhook"
    try:
        updates = bot_api(token, "getUpdates", {"limit": 100, "timeout": 0})
    except ValueError:
        return None, None, "updates"
    for update in reversed(updates):
        message = next((update[key] for key in ("message", "edited_message", "my_chat_member") if key in update), {})
        chat = message.get("chat", {})
        if chat.get("type") == "private" and (chat.get("username") or "").casefold() == recipient[1:].casefold():
            return str(chat["id"]), chat.get("username", ""), "private"
    return None, None, "not-found"


def setup():
    old = load_config()
    print("Token 输入不回显；直接按回车可保留现有值。")
    token = getpass.getpass("Bot Token：").strip() or old.get("token", "")
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Bot Token 格式无效")
    me = bot_api(token, "getMe")
    print(f"Bot 已验证：@{me.get('username', '未知')}")
    default = old.get("recipient", "")
    recipient = input(f"通知用户名或 chat_id [{default or '必填'}]：").strip() or default
    if not recipient:
        raise ValueError("请填写收件人")
    chat_id, username, kind = resolve_chat(token, recipient)
    if chat_id and recipient.startswith("@") and username.casefold() != recipient[1:].casefold():
        raise ValueError("chat_id 对应的用户名不匹配")
    if not chat_id:
        print("个人用户名无法直接接收 Bot 消息；当前 Bot 的 webhook 也不能用于 getUpdates。")
        numeric = input("请输入您与此 Bot 私聊的数字 chat_id（暂不知道可按回车稍后补填）：").strip()
        if numeric:
            chat_id, username, kind = resolve_chat(token, numeric)
            if username.casefold() != recipient.lstrip("@").casefold():
                raise ValueError("所填 chat_id 的用户名与收件人不一致")
    data = {"token": token, "recipient": recipient, "chat_id": chat_id or ""}
    save_config(data)
    if chat_id:
        print(f"Telegram 通知配置完成：{recipient}（{kind}），chat_id 已验证。")
    else:
        print("Bot Token 与用户名已保存；取得数字 chat_id 后重进此菜单补填，通知暂不启动。")


def configured():
    data = load_config()
    return bool(TOKEN_RE.fullmatch(data.get("token", "")) and re.fullmatch(r"-?[0-9]+", str(data.get("chat_id", ""))))


def proxy_info():
    try:
        return lookup_proxy_ip(18081)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def service_snapshot():
    active = command("systemctl", "is-active", "sing-box.service").stdout.strip()
    try:
        version_output = command("sing-box", "version").stdout.splitlines()
        version = version_output[0].split()[2] if version_output and len(version_output[0].split()) >= 3 else "未知"
    except OSError:
        version = "未知"
    interface = physical_interface()
    server_ip = local_ip(interface)
    protocol = "未知"
    tun = "未知"
    traffic = "未知"
    try:
        config = json.loads(SINGBOX_CONFIG.read_text())
        tags = {item.get("tag") for item in config.get("outbounds", [])}
        tags.update(item.get("tag") for item in config.get("endpoints", []))
        protocol = next((name for name, tag in (("HTTPS", "https-out"),
                        ("Shadowsocks", "shadowsocks-out"), ("WireGuard", "surfshark-wg"),
                        ("SOCKS5", "socks5-out")) if tag in tags), "未知")
        tun = "开启" if any(item.get("type") == "tun" for item in config.get("inbounds", [])) else "关闭（TProxy）"
        traffic = "分流" if config.get("route", {}).get("final") == "direct" else "全局"
    except (OSError, ValueError, KeyError):
        pass
    return active, version, server_ip, protocol, tun, traffic


def load_summary():
    try:
        result = json.loads((CONFIG.parent / "node-summary.json").read_text())
        return result if isinstance(result, dict) and result.get("version") == 1 else None
    except (OSError, ValueError):
        return None


def notify(event="periodic", snapshot=None, events=None):
    data = load_config()
    if not configured():
        raise ValueError("尚未填写有效 Bot Token 与 chat_id")
    active, version, server_ip, protocol, tun, traffic = service_snapshot()
    status = "正常" if active == "active" else f"异常（{active or '未知'}）"
    snapshot = snapshot if snapshot is not None else load_summary()
    special = events is not None
    selected = next((node for node in (snapshot or {}).get("nodes", [])
                     if node.get("id") == (snapshot or {}).get("active_id")), None)
    try:
        if special:
            # Fault alerts must not wait for an unresponsive proxy or IP provider.
            info = {"ip": selected["ip"], "city": selected.get("location_zh")} if selected and selected.get("ip") else None
        else:
            info = proxy_info() if active == "active" else None
        problem = None
    except ValueError as exc:
        info = None
        problem = str(exc)
        status = "异常（代理出口检测失败）"
    unavailable = bool((snapshot or {}).get("all_unavailable"))
    if unavailable:
        status = "异常（节点库无可用节点）"
    elif special:
        status = "事件通知；内核" + ("运行中" if active == "active" else "未运行")
    warning = unavailable or active != "active" or (not special and not info) or any(
        item.get("kind") == "connect_failed" for item in (events or []))
    lines = [
        f"{'🔴' if warning else '🟢'} sing-box 监测：{status}",
        f"服务器：{socket.gethostname()}",
        f"本机 IP：{server_ip}",
        f"内核版本：{version}",
        f"代理协议：{protocol} ｜ TUN：{tun}",
        f"流量模式：{traffic}",
        f"代理出口 IP{'（最近检测）' if special else ''}：{info['ip'] if info else '不可用'}",
    ]
    if info:
        place = " / ".join(str(info.get(key, "")).strip() for key in ("city", "region", "country") if info.get(key))
        lines.append(f"IP 地理位置：{place or '未知'}")
        if info.get("org"):
            lines.append(f"运营商：{info['org']}")
    elif problem:
        lines.append(f"检测错误：{problem[:180]}")
    lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if event == "started":
        lines.insert(1, "事件：代理已启用")
    lines[1:1] = event_lines(events)
    lines.extend(summary_lines(snapshot))
    chunks = split_message("\n".join(lines))
    message_ids = []
    for index, chunk in enumerate(chunks, 1):
        if index > 1:
            time.sleep(1)
        prefix = f"[{index}/{len(chunks)}] " if len(chunks) > 1 else ""
        result = bot_api(data["token"], "sendMessage", {"chat_id": data["chat_id"], "text": prefix + chunk})
        message_ids.append(result.get("message_id"))
    print(f"Telegram 通知已发送：{status}；消息 ID {','.join(map(str, message_ids))}", flush=True)
    return message_ids


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "setup":
            setup()
        elif len(sys.argv) == 2 and sys.argv[1] == "ready":
            sys.exit(0 if configured() else 1)
        elif len(sys.argv) in (2, 3) and sys.argv[1] == "notify":
            notify("started" if len(sys.argv) == 3 and sys.argv[2] == "started" else "periodic")
        else:
            raise ValueError("用法：telegram_monitor.py setup|ready|notify [started]")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Telegram 监测错误：{exc}", file=sys.stderr)
        sys.exit(1)
