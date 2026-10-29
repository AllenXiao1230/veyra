"""Live integration test. Run as xuan while recording/motion recording is OFF.

Temporarily switches between internal microphones, restarts audio/MediaMTX and
SIGSTOPs only the managed audio publisher. Restores the original source settings.
"""
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request


def request(path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("http://127.0.0.1:8094" + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.load(response)


def wait_healthy(previous_pid=None, source=None, timeout=55):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        try:
            last = request("/status")
            if last["healthy"] and (previous_pid is None or last["pid"] != previous_pid) and (
                    source is None or last["actual_source"] == source):
                return last
        except Exception as exc:
            last = {"error": str(exc)}
        time.sleep(0.5)
    raise RuntimeError("audio did not recover: " + json.dumps(last))


def main():
    with urllib.request.urlopen("http://127.0.0.1:8091/status", timeout=3) as response:
        recorder = json.load(response)
    assert not recorder["recording"] and not recorder["motion_enabled"], "Stop recording and motion recording first"
    initial = wait_healthy()
    saved = {"source": initial["selected_source"], "volume": initial["volume"], "muted": initial["muted"]}
    stopped_pid = None
    try:
        try:
            request("/source", {"source": "missing-integration-test"})
            raise AssertionError("accepted a nonexistent source")
        except urllib.error.HTTPError as error:
            assert error.code == 400
        assert request("/status")["selected_source"] == saved["source"]
        print("invalid source rejected without changing selection", flush=True)

        alternate = next((d for d in initial["sources"] if d["name"] != saved["source"] and
                          "pci-" in d["name"] and not d["name"].endswith(".monitor")), None)
        if alternate:
            request("/source", {"source": alternate["name"], "volume": min(100, alternate["volume"]),
                                "muted": alternate["muted"]})
            wait_healthy(previous_pid=initial["pid"], source=alternate["name"])
            print("alternate source selected and RTSP packets verified", flush=True)
            request("/source", saved)
            wait_healthy(source=saved["source"])

        before = request("/status")
        request("/restart", {})
        healthy = wait_healthy(previous_pid=before["pid"], source=saved["source"])
        print("manual restart recovered; source preserved", healthy["pid"], flush=True)

        stopped_pid = healthy["pid"]
        os.kill(stopped_pid, signal.SIGSTOP)
        recovered = wait_healthy_after_stall(stopped_pid, saved["source"])
        stopped_pid = None
        print("live hung publisher was replaced automatically", recovered["pid"], flush=True)

        before = request("/status")
        subprocess.run(["systemctl", "--user", "restart", "camera-mediamtx.service"], check=True, timeout=20)
        recovered = wait_healthy(previous_pid=before["pid"], source=saved["source"])
        print("MediaMTX restart recovered automatically", recovered["pid"], flush=True)

        subprocess.run(["systemctl", "--user", "restart", "camera-audio.service"], check=True, timeout=20)
        recovered = wait_healthy(source=saved["source"])
        assert {"source": recovered["selected_source"], "volume": recovered["volume"],
                "muted": recovered["muted"]} == saved
        print("audio service restart preserved source, volume and mute", flush=True)
    finally:
        if stopped_pid:
            try:
                os.kill(stopped_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        request("/source", saved)
        wait_healthy(source=saved["source"])


def wait_healthy_after_stall(pid, source):
    # A just-stopped publisher can retain its old healthy freshness for a few seconds.
    # Requiring a different PID ensures recovery happened rather than accepting that.
    return wait_healthy(previous_pid=pid, source=source)


if __name__ == "__main__":
    main()
