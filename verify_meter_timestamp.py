"""Record live timestamped input into an isolated folder; never control production APIs."""
import json
from pathlib import Path
import subprocess
import tempfile
import time

import recorder

folder = Path(tempfile.mkdtemp(prefix="pipeline-check-timestamp-", dir=Path(__file__).parent))
recorder.VIDEO_DIR = str(folder)
recorder.LOG_FILE = str(folder / "ffmpeg.log")
try:
    started, message = recorder.start_recording()
    assert started and recorder.is_recording(), message
    time.sleep(8)
finally:
    recorder.stop_recording()
files = list(folder.glob("*.mp4"))
assert len(files) == 1, files
video = files[0]
metadata = json.loads(subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,duration",
    "-of", "json", str(video)]))
assert {stream["codec_type"] for stream in metadata["streams"]} == {"video", "audio"}
# Eight wall-clock seconds must not become a sped-up three-second clip.
assert 7 <= float(metadata["format"]["duration"]) <= 12
durations = [float(stream["duration"]) for stream in metadata["streams"]]
assert max(durations) - min(durations) < 0.5, metadata
for second in (1, 2):
    subprocess.run(["ffmpeg", "-v", "error", "-ss", str(second), "-i", str(video),
                    "-frames:v", "1", str(folder / f"frame-{second}.png")], check=True, timeout=20)
subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"], check=True, timeout=30)
print(json.dumps({"folder": str(folder), "video": video.name, "metadata": metadata,
                  "recording_source": recorder.RECORDING_SOURCE, "motion_source": recorder.SOURCE}))
