"""Responsive terminal views shared by inventory and protocol selection."""

import os
import shutil
import sys
import time
import unicodedata

from node_notifications import clean, PROTOCOLS

PAGE_SIZE = 8


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


class View:
    def __init__(self, width=None, color=None):
        self.width = max(32, min(width or shutil.get_terminal_size((80, 24)).columns, 88))
        self.color = sys.stdout.isatty() and "NO_COLOR" not in os.environ if color is None else color
        self.lines = []

    def tint(self, text, color="cyan"):
        codes = {"cyan": "1;36", "green": "1;32", "red": "1;31", "yellow": "1;33", "dim": "2", "white": "1"}
        return f"\033[{codes[color]}m{text}\033[0m" if self.color else text

    def text(self, text="", color=None):
        # Strip control characters supplied by node names before adding our ANSI.
        text = clean(text, 2000)
        available = self.width - 4
        line = ""
        for char in text:
            if cell_width(line + char) > available:
                self.lines.append("  " + (self.tint(line, color) if color else line))
                line = ""
            line += char
        self.lines.append("  " + (self.tint(line, color) if color else line))

    def title(self, title):
        self.lines.append("")
        self.lines.append(self.tint("╭" + "─" * (self.width - 2) + "╮"))
        padding = max(0, self.width - 4 - cell_width(title))
        self.lines.append(self.tint("│ " + title + " " * padding + " │"))
        self.lines.append(self.tint("╰" + "─" * (self.width - 2) + "╯"))

    def section(self, title):
        self.text()
        self.text("── " + title + " " + "─" * max(0, self.width - 9 - cell_width(title)), "dim")

    def cards(self, records, active_id, page=0):
        pages = max(1, (len(records) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        self.section(f"节点列表  {page + 1}/{pages}")
        if not records:
            self.text("尚无节点 · 选择 01 批量导入", "yellow")
        for index in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(records))):
            record = records[index]
            current = record.get("id") == active_id and active_id is not None
            status, color = {"alive": ("代理已验证", "green"), "dead": ("失败", "red")}.get(record.get("status"), ("代理未验证", "yellow"))
            self.text(f"{index + 1:02d} {'▶ ' if current else ''}{record.get('custom_name') or record.get('name', '未命名')}", "white" if current else None)
            protocol = PROTOCOLS.get(record.get("node", {}).get("protocol"), "未知")
            latency = f"{record.get('latency_method') or '待测速'} {record['latency_ms']} ms" if record.get("latency_ms") is not None else "待检测"
            self.text(f"   {protocol} · {status} · {latency}", color)
            reach = {'reachable':'服务器可达', 'unreachable':'TCP 不可达'}.get(record.get('reachability'), '服务器延迟未知')
            self.text('   ' + reach, 'dim')
            self.text(f"   出口 {record.get('ip') or '待查询'}" + (" · 当前节点" if current else ""), "dim")
            if record.get("pending_node"):
                self.text("   有待应用修改 · 手动连接成功后生效", "yellow")
            if record.get("pending_error"):
                self.text("   上次应用失败 · 原配置保留", "red")
            if record.get("from_profile"):
                self.text("   来源：已填写的单节点配置（选择后加入节点库）", "dim")
            self.text()
        return page, pages


def render_inventory(data, page=0, width=None, color=None):
    view = View(width, color)
    nodes = data.get("nodes", [])
    current = next((n.get("custom_name") or n.get("name") for n in nodes if n.get("id") == data.get("active_id")), "未选择")
    alive = sum(n.get("status") == "alive" for n in nodes)
    dead = sum(n.get("status") == "dead" for n in nodes)
    view.title("多节点管理 · NODE CENTER")
    view.text("当前节点  " + current, "green" if data.get("active_id") else "dim")
    view.text(f"连接方式  {'自动 · 当前节点正常时保持连接' if data.get('auto') else '手动 · 故障时不自动切换'}")
    view.text(f"节点总览  {len(nodes)} 个 / 存活 {alive} / 失败 {dead} / 未检测 {len(nodes)-alive-dead}")
    view.text('备用巡检  分散轮询，每次一个；TCP 或实际连接失败立即清除', 'dim')
    view.text("连接检查  每分钟调度 · 成功缓存 2 分钟；失败用第二地址复核", "dim")
    view.text('选择规则  TCPing/Ping 升序；实际连接失败立即删除并尝试下一个', 'dim')
    view.text('WG 未回复 ping：延迟未知、最后尝试，不因 ping 失败删除', 'dim')
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(data["last_scan"])) if data.get("last_scan") else "尚未完成"
    view.text("上次巡检  " + when, "dim")
    page, pages = view.cards(nodes, data.get("active_id"), page)
    if pages > 1:
        view.text("11 上一页   12 下一页 · 节点编号跨页保持一致", "cyan")
    view.section("节点资料")
    for line in ("01 批量导入       02 查看节点", "03 编辑节点       09 修改名称", "08 删除节点"):
        view.text(line)
    view.section("连接与巡检")
    for line in ("04 选定节点并开启故障切换", "05 按 TCPing/Ping 自动连接", "06 关闭自动连接   07 全部服务器测速（不认证）", "10 设置备用节点复测间隔", "00 返回主菜单"):
        view.text(line, "cyan" if line.startswith("00") else None)
    view.text()
    return "\n".join(view.lines)


def render_protocol(data, protocol, records, page=0, editing=False):
    view = View()
    view.title(f"{PROTOCOLS[protocol]} · {'选择编辑节点' if editing else '选择代理节点'}")
    view.text("运行中节点的修改会保存为待应用。" if editing else "成功后开启故障自动切换；当前连接正常时保持该节点。", "dim")
    _, pages = view.cards(records, data.get("active_id"), page)
    if pages > 1:
        view.text(f"{len(records)+1:02d} 上一页   {len(records)+2:02d} 下一页", "cyan")
    view.text("00 返回主菜单", "cyan")
    view.text()
    return "\n".join(view.lines)
