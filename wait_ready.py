#!/usr/bin/python3
import json
import socket
import subprocess
import sys
import time

kind = sys.argv[1]
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    try:
        if kind == "tailscale":
            result = subprocess.run(["/usr/sbin/ip", "-j", "address", "show", "dev", "tailscale0"],
                                    capture_output=True, text=True, timeout=2)
            addresses = json.loads(result.stdout or "[]")
            ready = any(a.get("local") == "100.93.76.48" for device in addresses for a in device.get("addr_info", []))
        elif kind == "rtsp":
            with socket.create_connection(("127.0.0.1", 8554), timeout=1):
                ready = True
        elif kind == "audio":
            result = subprocess.run(["/usr/bin/ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                                     "-show_entries", "stream=codec_type", "-of", "json",
                                     "rtsp://127.0.0.1:8554/mic"],
                                    capture_output=True, text=True, timeout=3)
            ready = result.returncode == 0 and any(
                track.get("codec_type") == "audio" for track in json.loads(result.stdout).get("streams", []))
        else:
            raise ValueError("unknown readiness target")
        if ready:
            print(kind + " ready")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.5)
raise SystemExit(kind + " not ready within 45 seconds")
