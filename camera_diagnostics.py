"""Content-based local ingress checks. Never claims to test a remote network."""
import json
from pathlib import Path
import time
import urllib.request

PROBE = (b'Camera Monitor transport probe 0123456789\n' * 2048)


def read_state(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError,ValueError):
        return {} if default is None else default


def ingress_check(url='http://127.0.0.1:8090/backups-api/probe'):
    began = time.monotonic()
    try:
        request = urllib.request.Request(url,headers={'Range':'bytes=1024-2047'})
        with urllib.request.urlopen(request,timeout=5) as response:
            body = response.read(1025)
            valid = response.status == 206 and body == PROBE[1024:2048]
        return {'healthy':valid,'duration_ms':round((time.monotonic()-began)*1000),
                'scope':'local_nginx_range','remote_network_tested':False}
    except (OSError,ValueError) as error:
        return {'healthy':False,'error':type(error).__name__,'scope':'local_nginx_range','remote_network_tested':False}
