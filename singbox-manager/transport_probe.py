"""Direct, unauthenticated TCP connect / ICMP measurements only."""
import errno
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import time

CONFIG = Path('/etc/sing-box/config.json')


def direct_mark():
    try:
        for inbound in json.loads(CONFIG.read_text()).get('inbounds', []):
            if inbound.get('type') == 'tun' and inbound.get('auto_redirect'):
                mark = inbound.get('auto_redirect_output_mark', '0x2024')
                return int(mark, 0) if isinstance(mark, str) else int(mark)
    except (OSError, ValueError, TypeError):
        pass
    return 0x4002


def interface(family):
    result = subprocess.run(['ip', '-j', '-6' if family == socket.AF_INET6 else '-4',
                             'route', 'show', 'default', 'table', 'main'],
                            capture_output=True, text=True, timeout=3, check=True)
    for route in json.loads(result.stdout):
        if route.get('dev') and route['dev'] != 'lo':
            return route['dev']
    raise OSError('未找到物理默认路由')


def direct_socket(family, kind):
    sock = socket.socket(family, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface(family).encode() + b'\0')
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, direct_mark())
        return sock
    except BaseException:
        sock.close()
        raise


def skip_name(packet, offset):
    for _ in range(128):
        size = packet[offset]
        if size & 0xc0 == 0xc0:
            if offset + 1 >= len(packet):
                raise ValueError('DNS 压缩名称截断')
            return offset + 2
        if size & 0xc0 or size > 63:
            raise ValueError('DNS 名称无效')
        offset += 1
        if size == 0:
            return offset
        offset += size
    raise ValueError('DNS 名称过长')


def dns_answers(packet, identity):
    header = struct.unpack_from('!6H', packet)
    if header[0] != identity or not header[1] & 0x8000 or header[1] & 0x020f:
        raise ValueError('DNS 响应无效')
    offset = 12
    for _ in range(header[2]):
        offset = skip_name(packet, offset) + 4
    addresses = []
    for _ in range(header[3]):
        offset = skip_name(packet, offset)
        kind, cls, _, size = struct.unpack_from('!HHIH', packet, offset)
        offset += 10
        raw = packet[offset:offset + size]
        if cls == 1 and (kind, size) in ((1, 4), (28, 16)) and len(raw) == size:
            addresses.append(str(ipaddress.ip_address(raw)))
        offset += size
    return addresses


def resolve(host):
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    labels = host.rstrip('.').encode('idna').split(b'.')
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError('节点服务器域名无效')
    question = b''.join(bytes([len(label)]) + label for label in labels) + b'\0'
    # Query public DNS through marked sockets, avoiding TUN fake-IP answers.
    for resolver in ('1.1.1.1', '8.8.8.8', '2606:4700:4700::1111'):
        family = socket.AF_INET6 if ':' in resolver else socket.AF_INET
        try:
            with direct_socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(2)
                sock.connect((resolver, 53))
                identity = secrets.randbelow(65536)
                qtype = 28 if family == socket.AF_INET6 else 1
                sock.send(struct.pack('!6H', identity, 0x100, 1, 0, 0, 0) + question + struct.pack('!HH', qtype, 1))
                result = dns_answers(sock.recv(4096), identity)
                if result:
                    return result[:2]
        except (OSError, ValueError, IndexError, struct.error, subprocess.SubprocessError):
            continue
    raise OSError('节点域名直连解析失败')


def measure(node):
    wg = node['protocol'] == 'wg'
    host = node['peer_address'] if wg else node['server']
    port = int(node['peer_port'] if wg else node['server_port'])
    method = 'ping' if wg else 'tcping'
    base = {'latency_method': method, 'latency_ms': None, 'latency_checked_at': int(time.time())}
    try:
        addresses = resolve(host)
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(base, reachability='unknown', transport_error='直连 DNS 或路由暂不可用')
    for address in addresses:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        try:
            if wg:
                command = ['ping', '-6' if family == socket.AF_INET6 else '-4', '-n', '-c', '1', '-W', '2',
                           '-I', interface(family), '-m', str(direct_mark()), address]
                result = subprocess.run(command, capture_output=True, text=True, timeout=4,
                                        env=dict(os.environ, LC_ALL='C'))
                found = re.search(r'time[=<]([\d.]+)\s*ms', result.stdout)
                if result.returncode == 0 and found:
                    return dict(base, reachability='reachable', latency_ms=float(found[1]))
            else:
                try:
                    connection = direct_socket(family, socket.SOCK_STREAM)
                except (OSError, ValueError, subprocess.SubprocessError):
                    return dict(base, reachability='unknown', transport_error='本机直连路由或标记暂不可用')
                with connection as sock:
                    sock.settimeout(3)
                    start = time.monotonic()
                    sock.connect((address, port))
                    # Deliberately no send(), TLS, proxy authentication or HTTP.
                    return dict(base, reachability='reachable', latency_ms=round((time.monotonic()-start)*1000, 2))
        except OSError as exc:
            if exc.errno in (errno.ENETUNREACH, errno.EPERM, errno.EACCES, errno.ENODEV):
                return dict(base, reachability='unknown', transport_error='本机直连网络暂不可用')
            continue
        except subprocess.SubprocessError:
            continue
    return dict(base, reachability='unknown' if wg else 'unreachable',
                transport_error='未收到 ICMP 回复（不能据此判死）' if wg else 'TCP 握手失败')
