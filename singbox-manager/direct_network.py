"""Direct networking used only to distinguish local outages from proxy failure."""
import ipaddress
import json
from pathlib import Path
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



def require_internet():
    """Never delete nodes while direct Internet health is unknown."""
    import ssl
    from deadline import deadline
    from probe_policy import Deferred
    try:
        with deadline(24):
            for host in ('www.gstatic.com', 'cp.cloudflare.com'):
                try:
                    addresses = resolve(host)
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
                for address in addresses[:1]:
                    try:
                        family = socket.AF_INET6 if ':' in address else socket.AF_INET
                        with direct_socket(family, socket.SOCK_STREAM) as raw:
                            raw.settimeout(4)
                            raw.connect((address, 443))
                            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as sock:
                                sock.sendall(('GET /generate_204 HTTP/1.1\r\nHost: '+host+'\r\nConnection: close\r\n\r\n').encode())
                                response = b''
                                while b'\r\n' not in response and len(response)<4096:
                                    chunk = sock.recv(512)
                                    if not chunk:
                                        break
                                    response += chunk
                                if response.split(b'\r\n',1)[0].split()[1:2] == [b'204']:
                                    return
                    except TimeoutError:
                        raise
                    except (OSError, ValueError, subprocess.SubprocessError):
                        continue
    except TimeoutError:
        pass
    raise Deferred('本机直连网络或检测网站暂不可用，保留所有节点', time.time()+180, 'local')
