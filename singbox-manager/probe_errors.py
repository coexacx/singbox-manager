"""Classify explicit upstream errors without exposing log contents."""
import re
import time
from probe_policy import Restricted

def upstream_restriction(log):
    # Only HTTP proxy outbound messages prove that the *provider* rejected CONNECT.
    lines='\n'.join(l.lower() for l in log.splitlines() if 'outbound/http' in l or 'using outbound/http' in l)
    if re.search(r'(unexpected status|status code).*\b429\b',lines):
        return Restricted('上游代理明确限流；本机暂停真实代理检测',time.time()+3600,'provider_rate')
    return None
