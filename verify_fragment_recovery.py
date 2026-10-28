"""Force-stop only an isolated test encoder and verify its persisted MP4 headers."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import recorder

folder = tempfile.mkdtemp(prefix="fragment-check-", dir=Path(__file__).parent)
recorder.VIDEO_DIR = folder
recorder.LOG_FILE = folder + "/ffmpeg.log"
try:
    recorder.start_recording()
    time.sleep(5)
    process = recorder.recorder
    process.kill()
    process.wait(timeout=5)
    time.sleep(0.1)
    assert not recorder.is_recording() and recorder.recording_state == "failed"
    files = list(Path(folder).glob("*.mp4"))
    assert files
    for file in files:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type:format=duration", "-of", "json", str(file)], capture_output=True, text=True, timeout=8)
        metadata = json.loads(result.stdout or "{}")
        assert result.returncode == 0 and metadata.get("streams"), result.stderr
        print("forced encoder exit: fragment still parsable", metadata)
finally:
    recorder.stop_recording()
