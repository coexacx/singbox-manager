#!/usr/bin/env python3
"""Remove and verify only networking owned by this manager after service stop."""
import json
from pathlib import Path
import subprocess
import sys
import transparent

CONFIG = Path('/etc/sing-box/config.json')


def run(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=4, check=check)


def owned_rule(rule):
    table = str(rule.get('table', ''))
    mark = str(rule.get('fwmark', ''))
    priority = rule.get('priority')
    if table == '18082' and priority == 18082 and mark in ('0x4001', '0x4001/0x4001'):
        return True
    if table == '2022' and mark in ('0x2023', '0x2024') and priority in (9001, 32768):
        return True
    if priority == 9000 and mark == '0x2024' and rule.get('goto') == 9002:
        return True
    return priority == 9002 and 'nop' in rule and rule.get('src') == 'all'


def restore():
    state = run('systemctl', 'show', '--property=ActiveState', '--value', 'sing-box.service').stdout.strip()
    if state not in ('inactive', 'failed'):
        raise RuntimeError('sing-box 尚未停止，无法确认直连')
    transparent.cleanup()
    # The reserved interface plus its literal nft reference identify our TUN.
    tables = json.loads(run('nft', '-j', 'list', 'tables').stdout).get('nftables', [])
    for item in tables:
        table = item.get('table', {})
        if table.get('family') == 'inet' and table.get('name') == 'sing-box':
            rules = run('nft', '-j', 'list', 'table', 'inet', 'sing-box').stdout
            if '"sb-split0"' not in rules:
                raise RuntimeError('发现非本管理器的 sing-box 规则，未自动清除')
            run('nft', 'delete', 'table', 'inet', 'sing-box')
    if run('ip', 'link', 'show', 'sb-split0', check=False).returncode == 0:
        run('ip', 'link', 'delete', 'sb-split0')
    for family in ('-4', '-6'):
        rules = json.loads(run('ip', '-j', family, 'rule', 'show').stdout)
        for rule in rules:
            if owned_rule(rule):
                # All identifying selectors accompany the priority, so unrelated
                # policy routing at the same priority is not removed.
                args = ['ip', family, 'rule', 'del', 'pref', str(rule['priority'])]
                if 'not' in rule:
                    args.append('not')
                if 'fwmark' in rule:
                    args += ['fwmark', rule['fwmark']]
                if 'table' in rule:
                    args += ['lookup', str(rule['table'])]
                elif 'goto' in rule:
                    args += ['goto', str(rule['goto'])]
                elif 'nop' in rule:
                    args.append('nop')
                run(*args)
        remaining = json.loads(run('ip', '-j', family, 'rule', 'show').stdout)
        if any(owned_rule(rule) or str(rule.get('table', '')) in ('2022', '18082') for rule in remaining):
            raise RuntimeError('仍存在管理器策略路由')
    remaining = json.loads(run('nft', '-j', 'list', 'tables').stdout).get('nftables', [])
    if any(i.get('table', {}).get('name') in ('sing-box', 'singbox_manager') and i['table'].get('family') == 'inet' for i in remaining):
        raise RuntimeError('仍存在透明接管规则')
    if run('ip', 'link', 'show', 'sb-split0', check=False).returncode == 0:
        raise RuntimeError('TUN 接口仍存在')
    print('直连恢复检查通过：服务已停止，TUN 和透明接管规则已清理。', flush=True)


if __name__ == '__main__':
    from utf8_runtime import ensure_utf8
    ensure_utf8()
    try:
        restore()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('直连恢复未完成：' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
