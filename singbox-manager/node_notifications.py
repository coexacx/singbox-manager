"""Credential-free node snapshots and Telegram message formatting."""

import time

PROTOCOLS = {"wg": "WireGuard", "https": "HTTPS", "shadowsocks": "Shadowsocks", "socks5": "SOCKS5"}
STATUSES = {"alive": "代理已验证", "dead": "失败", "unknown": "代理未验证"}


def clean(value, limit=100):
    return "".join(c for c in str(value or "") if c.isprintable())[:limit]


def public_snapshot(data):
    """Allowlist fields: never copy node configs, tokens or passwords."""
    nodes = []
    for record in data.get("nodes", []):
        nodes.append({
            "id": clean(record.get("id")),
            "name": clean(record.get("custom_name") or record.get("name")),
            "protocol": clean(record.get("node", {}).get("protocol")),
            "status": record.get("status", "unknown"),
            "ip": clean(record.get("ip")),
            "location_zh": clean(record.get("location_zh")),
            "checked_at": record.get("checked_at"),
            "pending_changes": bool(record.get("pending_node")),
        })
    return {
        "version": 1, "nodes": nodes, "active_id": data.get("active_id"),
        "auto": bool(data.get("auto")),
        "exhausted": bool(data.get("exhausted")),
        "direct_restored": bool(data.get("direct_restored")),
        "direct_restore_pending": bool(data.get("direct_restore_pending")),
        "last_active_check": data.get("last_active_check"), "updated_at": int(time.time()),
        "all_unavailable": bool(data.get("availability_alert")),
        "pending_count": len(data.get("pending_notifications", [])),
    }


def summary_lines(snapshot):
    if snapshot is None:
        return []
    nodes = snapshot.get("nodes", [])
    active = next((node for node in nodes if node.get("id") == snapshot.get("active_id")), None)
    alive = sum(node.get("status") == "alive" for node in nodes)
    dead = sum(node.get("status") == "dead" for node in nodes)
    lines = [
        f"当前节点：{clean(active.get('name')) if active else '未选择节点库节点'}",
        f"自动连接：{'开启' if snapshot.get('auto') else '关闭'}",
        f"节点总览：共 {len(nodes)} 个 / 已验证 {alive} / 失败 {dead} / 未验证 {len(nodes)-alive-dead}",
        '连接规则：按列表顺序尝试；实际连接失败删除；备用节点不检测',
    ]
    if snapshot.get("all_unavailable"):
        lines.append("⚠️ 节点库当前无可用节点" if nodes else "⚠️ 节点库已无可用节点，需导入新节点")
    if snapshot.get('direct_restore_pending'):
        lines.append('⚠️ 恢复直连尚未完成，后台重试清理')
    elif snapshot.get('direct_restored'):
        lines.append('sing-box 已停止，已恢复直连；等待添加并选择新节点')
    if snapshot.get('last_active_check'):
        lines.append('最近当前节点检测：' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(snapshot['last_active_check'])))
    if snapshot.get("pending_count"):
        lines.append(f"待重试事件通知：{snapshot['pending_count']} 条")
    lines.append("── 节点明细（最近检测结果）──")
    for index, node in enumerate(nodes, 1):
        current = " [当前]" if node.get("id") == snapshot.get("active_id") else ""
        lines.append(f"{index}. {clean(node.get('name'))}{current}")
        if node.get("pending_changes"):
            lines.append("   存在待应用修改；自动巡检使用原配置")
        lines.append(f"   {PROTOCOLS.get(node.get('protocol'), '未知')} | {STATUSES.get(node.get('status'), '未知')} | IP {clean(node.get('ip')) or '待查询'}")
    return lines


def event_lines(events):
    titles = {"direct_fallback": "全部节点失败，已恢复直连", "direct_restore_failed": "全部节点失败，恢复直连未完成", "probe_deferred": "检测暂缓（未计失败）", "connected": "节点连接成功", "auto_connected": "自动连接成功", "failover": "自动故障切换成功",
              "connect_failed": "节点连接失败", "deleted": "节点已删除", "all_unavailable": "全部节点不可用",
              "recovered": "节点可用性恢复", "stopped": "代理已手动关闭", "stop_failed": "关闭代理失败",
              "mode_changed": "运行模式切换成功", "mode_failed": "运行模式切换失败", "edited": "节点配置已保存"}
    lines = []
    for event in events or []:
        kind = event.get("kind")
        if kind not in titles:
            continue
        lines.append(f"事件：{titles[kind]}")
        if event.get("time"):
            lines.append("事件时间：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["time"])))
        if event.get("reason"):
            lines.append("原因：" + clean(event["reason"], 240))
        if event.get("previous") or event.get("current"):
            if event.get("previous") == event.get("current"):
                lines.append("节点：" + clean(event.get("current")))
            else:
                lines.append(f"节点：{clean(event.get('previous')) or '未连接'} → {clean(event.get('current')) or '未连接'}")
        if event.get("failed"):
            lines.append("连接失败的候选：" + "、".join(clean(item) for item in event["failed"]))
        for item in event.get("deleted", []):
            lines.append(f"删除：{clean(item.get('name'))}")
    return lines


def split_message(text, limit=3500):
    """Stay below Telegram's limit, including non-BMP emoji UTF-16 units."""
    chunks, current, size = [], [], 0
    for char in text:
        width = len(char.encode("utf-16-le")) // 2
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks
