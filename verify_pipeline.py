"""Live inputs/encoders, isolated output folder; never controls production APIs."""
import json
import pathlib
import subprocess
import tempfile
import time
import urllib.request

import recorder

folder = tempfile.mkdtemp(prefix="pipeline-check-", dir=pathlib.Path(__file__).parent)
recorder.VIDEO_DIR = folder
recorder.LOG_FILE = folder + "/ffmpeg.log"
try:
    changed, message = recorder.start_recording()
    assert changed and recorder.is_recording(), message
    time.sleep(3)
finally:
    recorder.stop_recording()
files = list(pathlib.Path(folder).glob("*.mp4"))
assert files, "encoder produced no MP4"
for file in files:
    metadata = json.loads(subprocess.check_output([
        "/usr/bin/ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name", "-of", "json", str(file)]))
    assert {s["codec_type"] for s in metadata["streams"]} == {"audio", "video"}
    assert float(metadata["format"]["duration"]) > 0
    print("isolated MP4 verified", file.name, metadata)
recorder.AUDIO_SOURCE = "rtsp://127.0.0.1:8554/does-not-exist-test"
try:
    recorder.start_recording()
    raise AssertionError("missing audio incorrectly reported success")
except RuntimeError:
    assert recorder.recording_state == "failed"
    assert not recorder.is_recording()
    print("missing audio correctly failed", recorder.last_error[-200:])
finally:
    recorder.stop_recording()

process = subprocess.Popen(["/usr/bin/python3", "-c",
    "import timestamp_v2 as t; t.PORT=18093; t.main()"], cwd=pathlib.Path(__file__).parent)
try:
    state = None
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18093/health", timeout=2) as response:
                state = json.load(response)
                if state["healthy"]:
                    break
        except Exception:
            time.sleep(0.25)
    assert state and state["healthy"]
    first = state["frame_id"]
    time.sleep(0.5)
    with urllib.request.urlopen("http://127.0.0.1:18093/health", timeout=2) as response:
        state = json.load(response)
    assert state["frame_id"] > first
    with urllib.request.urlopen("http://127.0.0.1:18093/stream", timeout=3) as response:
        assert b"image/jpeg" in response.read(1024)
    print("isolated preview health and streaming verified", state)
finally:
    process.terminate()
    process.wait(timeout=10)
